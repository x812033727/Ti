"""Autopilot —— Ti Studio 的自主自我改善迴圈（獨立程序：`python -m studio.autopilot`）。

迴圈：取 backlog pending 任務 → 在 working clone 跑 headless 討論（專家在沙箱內改 Ti
自己）→ 跑完整 pytest 當閘門 → 綠才 commit/push/squash-merge 進 main → 重佈 ti.service
（含健康檢查+自動回滾）→ 把討論發現的後續任務寫回 backlog → 下一個。backlog 空時跑
自我評估產生新任務。可隨時用暫停開關（pause 檔）叫停；改到自身程式碼後 os.execv 重載。

長跑不間斷：迴圈頂端有額度閘門（provider_quota.gate）——全部 provider 額度受限時睡到
最早重置再重查，而非空轉燒失敗；provider 中途不可用只把任務退回 pending（不寫 pause 檔）。
每輪把 {state, task_id, sleep_until, quota…} 心跳寫進 <state dir>/status.json 供 /api/autopilot 觀測。

獨立於 ti.service 跑,所以重佈（restart ti.service）不會打斷自己；狀態存在 backlog 檔,
崩潰/重啟可續跑。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

from . import backlog, config, deploy, history, provider_quota, publisher, runner, secure_write
from .orchestrator import StudioSession, parse_tasks

# repo identity 正規化的單一真相（host-aware，同 path 非 GitHub host 視為不同）已抽至
# repo_ident 模組；保留 `_repo_key` 名稱，既有守護測試與呼叫點不變。
from .repo_ident import repo_key as _repo_key

log = logging.getLogger("ti.autopilot")

# 心跳檔寫入唯一 choke point：與 backlog/history 同範式走 secure_write.secure_write_root
# （原子 tmp+rename + owner 驗證）。module-level alias 兼顧可被測試 monkeypatch。
secure_write_root = secure_write.secure_write_root

_GH = ["gh"]
_GIT_CRED = ["-c", "credential.helper=!gh auth git-credential"]
# 自我重載：autopilot 跑討論依賴整個 studio 套件（orchestrator／experts／flow／providers…），
# 故監看整包 studio/*.py 的 mtime——只盯少數檔會漏掉 orchestrator-only 的部署（如 #218），
# 讓 autopilot 一直跑舊 orchestration 邏輯（self-reload 在任務之間做、安全）。


async def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        runner.kill_process_group(proc)
        return -1, f"(逾時 {timeout}s)"
    return proc.returncode if proc.returncode is not None else -1, out.decode("utf-8", "replace")


def _self_sig() -> float:
    """整個 studio 套件（top-level *.py）的最新 mtime 總和，用來判斷部署後是否需重載自己。

    涵蓋整包而非少數檔:任何被 autopilot 依賴的模組（orchestrator/experts/flow/conclusion/
    providers…）更新都觸發 reload,避免 orchestrator-only 的 PR 部署後 autopilot 仍跑舊邏輯。
    """
    base = Path(__file__).resolve().parent
    total = 0.0
    for path in base.glob("*.py"):
        with contextlib.suppress(OSError):
            total += path.stat().st_mtime
    return total


# --- working clone -------------------------------------------------------


async def _prepare_clone() -> str:
    """確保 working clone 存在且重置到 origin/<branch> 的乾淨狀態。回傳路徑。"""
    work = str(config.AUTOPILOT_WORK_DIR)
    url = f"https://github.com/{config.AUTOPILOT_REPO}.git"
    branch = config.AUTOPILOT_BRANCH
    if not (Path(work) / ".git").exists():
        Path(work).parent.mkdir(parents=True, exist_ok=True)
        rc, out = await _run(["git", *_GIT_CRED, "clone", url, work], timeout=300)
        if rc != 0:
            raise RuntimeError(f"clone 失敗：{out[-400:]}")
    await _run(["git", *_GIT_CRED, "fetch", "origin", branch], cwd=work, timeout=120)
    await _run(["git", "checkout", "-q", branch], cwd=work, timeout=60)
    await _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=work, timeout=60)
    await _run(["git", "clean", "-fdq"], cwd=work, timeout=60)
    # 本地 commit 身分（workspace commit 用）
    await _run(["git", "config", "user.email", "noreply@anthropic.com"], cwd=work)
    await _run(["git", "config", "user.name", "Ti Autopilot"], cwd=work)
    return work


# --- 測試閘門 + merge ----------------------------------------------------


async def _gate_tests(clone: str) -> tuple[bool, str]:
    """在 working clone 跑完整 pytest（沙箱內）。綠才回 True。"""
    # 固定指令走參數式 exec：argv 不經 shell，metacharacter 天然安全。
    # 用 sys.executable（當前直譯器絕對路徑）而非裸 "python"：避免 PATH 無 `python`
    # （多數環境僅有 `python3`）導致 exec 解析失敗；同時保證用同一直譯器跑 pytest。
    # timeout/sandbox 顯式帶齊（run_command_exec 預設 sandbox=None 會走 fail-closed）。
    result = await runner.run_command_exec(
        clone,
        [sys.executable, "-m", "pytest", "-q"],
        timeout=600,
        sandbox=True,
        label="pytest gate",
    )
    # 標籤計入截尾預算（總長維持 ≤1500、尾段保留）：先留出前綴長度再截尾。
    prefix = "[test] "
    return result.ok, prefix + result.output[-(1500 - len(prefix)) :]


async def _gate_lint(clone: str) -> tuple[bool, str]:
    """對齊 CI lint job：ruff check + ruff format --check。

    討論／pytest 閘門都不跑 ruff，lint 問題（未用 import、格式漂移等）會一路綠燈進
    main 卻在 GitHub CI 紅。此閘門補上。ruff 未安裝時 fail-open（只記警告不擋），避免
    部署環境缺 ruff 害死所有任務；裝了 ruff 才硬性把關。
    """
    probe = await runner.run_command_exec(
        clone,
        [sys.executable, "-m", "ruff", "--version"],
        timeout=30,
        sandbox=True,
        label="ruff probe",
    )
    if not probe.ok:
        log.warning("ruff 未安裝，略過 lint 閘門（請在部署環境 pip install ruff 以啟用）")
        return True, "[lint] ruff 缺失，略過 lint 閘門"
    for argv, name in (
        ([sys.executable, "-m", "ruff", "check", "."], "ruff check"),
        ([sys.executable, "-m", "ruff", "format", "--check", "."], "ruff format --check"),
    ):
        r = await runner.run_command_exec(clone, argv, timeout=120, sandbox=True, label=name)
        if not r.ok:
            # 標籤計入截尾預算（與 _gate_tests/_gate_collect 一致，總長維持 ≤1200）。
            prefix = f"[lint] {name} 未過：\n"
            return False, prefix + r.output[-(1200 - len(prefix)) :]
    return True, "[lint] ruff OK"


async def _gate_collect_without_sdk(clone: str) -> tuple[bool, str]:
    """對齊 CI test job 環境（刻意不裝 claude_agent_sdk）跑 pytest collection。

    autopilot 自身環境裝了 SDK，故 gate 的 pytest collection 永遠成功，對「頂層 import
    SDK 才會炸」的 CI collection error 是盲的。此閘門用 sys.modules 封鎖 SDK 重現該環境，
    及早攔截模組級 import 耦合。
    """
    code = (
        "import sys; sys.modules['claude_agent_sdk'] = None; "
        "sys.exit(__import__('pytest').main("
        "['--collect-only', '-q', '-p', 'no:cacheprovider', 'tests/']))"
    )
    r = await runner.run_command_exec(
        clone, [sys.executable, "-c", code], timeout=180, sandbox=True, label="collect (no SDK)"
    )
    # 標籤計入截尾預算（與 _gate_tests/_gate_lint 一致，總長維持 ≤1200）。
    prefix = "[collect] "
    return r.ok, prefix + r.output[-(1200 - len(prefix)) :]


async def _check_branch_protection(clone: str, branch: str) -> tuple[str, str]:
    """查詢 `branch`（應傳合併目標 main）的保護狀態，回傳三態 (state, detail)。

    state ∈ {"protected", "unprotected", "unknown"}：
      - protected：偵測到分支保護規則（Rulesets 非空 或 舊 protection 200）。
      - unprotected：明確無保護（Rulesets 回空陣列 `[]`，或舊端點 HTTP 404）。
      - unknown：無法確認（HTTP 403 無權限／網路失敗／逾時 rc=-1／其他）。

    優先打新一代 Rulesets 端點 `repos/{repo}/rules/branches/{branch}`（現代設定、
    無 404 陷阱、多半不需 Administration:read）；舊 `branches/{branch}/protection`
    為輔。任一端點判定 protected 即回 protected。

    判讀優先序寫死——先看 rc==0 的內容，再看字串：
      rc==0 → 解析 JSON，空陣列→unprotected、非空→protected；
      rc≠0 → 含「HTTP 404」→unprotected、含「HTTP 403」或逾時/其他→unknown。
    未匹配任何已知狀況一律 default 落 unknown（保守兜底）。
    """
    repo = config.AUTOPILOT_REPO

    # --- 主：Rulesets 端點 ---------------------------------------------------
    # rules_clean 追蹤「Rulesets 是否被乾淨確認為空」：唯有主端點 rc==0 且回合法空 list
    # 才為 True。只有 rules_clean 時，後續舊端點 404 才允許判 unprotected——主端點任何
    # 錯誤（5xx／連線中斷等非 403、非逾時、非 404）即使舊端點恰好 404 也絕不放行。
    rules_clean = False
    rc, out = await _run(
        [*_GH, "api", f"repos/{repo}/rules/branches/{branch}"], cwd=clone, timeout=60
    )
    if rc == 0:
        try:
            rules = json.loads(out)
        except (ValueError, TypeError):
            rules = None
        if isinstance(rules, list):
            if rules:
                return "protected", f"Rulesets：{len(rules)} 條規則套用於 {branch}"
            # 空陣列＝Rulesets 乾淨確認無規則；仍以舊 protection 端點兜傳統保護設定
            rules_clean = True
        # 非 list（非預期格式）→ rules 未乾淨確認，往下用舊端點且不得單憑 404 放行
    elif "HTTP 403" in out:
        return "unknown", f"Rulesets 端點 403（無 Administration:read 權限？）：{out[-200:]}"
    elif rc == -1 or "逾時" in out:
        return "unknown", f"Rulesets 端點逾時/網路失敗：{out[-200:]}"
    # 其餘 rc≠0（5xx／連線錯誤／404 等）：rules 未乾淨確認，續查舊端點但保守不放行

    # --- 輔：舊 branch-protection 端點 --------------------------------------
    rc2, out2 = await _run(
        [*_GH, "api", f"repos/{repo}/branches/{branch}/protection"], cwd=clone, timeout=60
    )
    if rc2 == 0:
        # 200＝有傳統分支保護（明確正向訊號，與 rules_clean 無關）
        return "protected", f"舊 protection 端點回 200（{branch} 受傳統分支保護）"
    # 唯一放行（unprotected）出口：三重條件——(1) 主端點 Rulesets 已乾淨確認為空、
    # (2) 舊端點失敗 rc、(3) 明確 HTTP 404（且不含 403，避免單一輸出雙碼誤判）。
    # 主端點若是 5xx／連線錯誤等未乾淨確認，即使舊端點 404 也落 unknown，不 fall-through。
    if rules_clean and rc2 != 0 and "HTTP 404" in out2 and "403" not in out2:
        return "unprotected", f"{branch} 無 Rulesets 規則且無傳統分支保護（404）"
    if "HTTP 403" in out2:
        return "unknown", f"舊 protection 端點 403（無 admin 權限？）：{out2[-200:]}"
    if rc2 == -1 or "逾時" in out2:
        return "unknown", f"舊 protection 端點逾時/網路失敗：{out2[-200:]}"

    # 兜底：Rulesets 未乾淨確認（主端點錯誤）、或舊端點非 404/403/逾時的未知組合 → 保守 unknown
    return "unknown", (
        f"無法確認保護狀態（rules rc={rc} clean={rules_clean}, protection rc={rc2}）：{out2[-200:]}"
    )


class MergeResult(tuple):
    """`(ok, msg)` 二元組的向後相容擴充：額外攜帶 pr_number / branch 供落檔追溯。

    刻意繼承 tuple 並固定只裝兩個元素——既有呼叫端（含大量守護測試）的
    `ok, msg = await _commit_push_merge(...)` 解包完全不受影響；需要 PR 編號／
    合併分支的呼叫端（run_one_task 落檔 pr/merged_branch）用屬性取。
    （不設 __slots__：tuple 子類不支援非空 __slots__，屬性走一般 __dict__。）
    """

    def __new__(cls, ok: bool, msg: str, *, pr_number: int | None = None, branch: str = ""):
        self = super().__new__(cls, (ok, msg))
        return self

    def __init__(self, ok: bool, msg: str, *, pr_number: int | None = None, branch: str = ""):
        self.pr_number = pr_number
        self.branch = branch


async def _commit_push_merge(clone: str, task: dict) -> tuple[bool, str]:
    """把成果開分支、push、squash-merge 進 main。dryrun 只回報。

    成功（已合併）時回傳 MergeResult——解包仍是 (True, msg)，另帶 pr_number / branch
    屬性供 run_one_task 落檔（呼叫端以 getattr 容錯讀取，失敗路徑維持純 tuple）。
    """
    repo = (config.AUTOPILOT_REPO or "").strip()
    publish_repo = (config.PUBLISH_REPO or "").strip()
    repo_key = _repo_key(repo)
    if not repo_key:
        return False, "AUTOPILOT_REPO 未設定，已中止推送"
    if publish_repo and _repo_key(publish_repo) != repo_key:
        return False, (
            "PUBLISH_REPO 與 AUTOPILOT_REPO 指向不同 repo，為避免污染專案 repo，已中止推送"
        )
    # owner allowlist 護欄：AUTOPILOT_REPO 的 owner 不在 allowlist（TI_PUBLISH_OWNER_ALLOWLIST）
    # 內即中止，維持「違反不變式回 (False, reason)、不執行任何 push/PR/merge」的既有合約。
    try:
        publisher.assert_repo_allowed(repo)
    except ValueError as e:
        return False, str(e)

    token = publisher.set_repo_override(repo)
    try:
        branch = f"autopilot/task-{task['id']}"
        title = f"autopilot: {task['title']}"[:72]
        # 確保所有變更都已 commit（session 通常已 commit，這裡兜底）
        await _run(["git", "checkout", "-B", branch], cwd=clone, timeout=60)
        await _run(["git", "add", "-A"], cwd=clone, timeout=60)
        rc, out = await _run(
            [
                "git",
                "-c",
                "user.email=noreply@anthropic.com",
                "-c",
                "user.name=Ti Autopilot",
                "commit",
                "-m",
                title,
                "--author=Claude <noreply@anthropic.com>",
            ],
            cwd=clone,
            timeout=60,
        )
        # rc!=0 且訊息為 nothing to commit 仍可能 branch == main，視為無變更
        rc_diff, diff = await _run(
            ["git", "rev-list", "--count", f"origin/{config.AUTOPILOT_BRANCH}..HEAD"],
            cwd=clone,
            timeout=30,
        )
        if diff.strip() in ("", "0"):
            return False, "沒有產生任何變更（無 commit 可合併）"

        if config.AUTOPILOT_DRYRUN:
            return True, f"[dryrun] 會 push {branch} 並 squash-merge 進 {config.AUTOPILOT_BRANCH}"

        # push 前防呆：每個 task 都是全新分支，遠端不該已存在同名分支。三態判定——
        #   rc!=0：ls-remote 本身失敗（網路/認證），視為錯誤中止，不可 fall-through 當「不存在」。
        #   rc==0 且有輸出：遠端已存在同名分支（task 重跑或殘留），預設中止；FORCE_PUSH 為真才放行覆寫。
        #   rc==0 且空輸出：遠端不存在，放行。
        rc, out = await _run(
            ["git", *_GIT_CRED, "ls-remote", "--heads", "origin", branch],
            cwd=clone,
            timeout=60,
        )
        if rc != 0:
            return False, f"ls-remote 檢查失敗（無法確認遠端狀態，已中止）：{out[-400:]}"
        if out.strip() and not config.AUTOPILOT_FORCE_PUSH:
            return False, (
                f"遠端已存在同名分支 {branch}，為避免覆寫已中止；"
                f"如確認要覆寫殘留分支，設 TI_AUTOPILOT_FORCE_PUSH=1"
            )

        # 第二道防線（與上方 ls-remote 防覆寫各自獨立）：merge 進 main 前，主動查「合併目標
        # AUTOPILOT_BRANCH」的保護狀態。放在 push 之前——unknown 時 fail-safe 中止且尚未 push，
        # 不留遠端孤兒分支。dryrun 已於前面提早 return，此處天然不打 API。
        # 唯一硬規則：state=="unknown" 一律中止（絕不 fall-through 當無保護）；protected/
        # unprotected 皆放行（受保護分支由後續 publisher._merge_flow「等 CI→合併」自然攔下：
        # 必過檢查未滿足會回 BLOCKED 並關 PR，不在此重複把關）。
        if config.AUTOPILOT_PROTECTION_CHECK:
            state, detail = await _check_branch_protection(clone, config.AUTOPILOT_BRANCH)
            if state == "unknown":
                return False, (
                    f"無法確認保護狀態（{config.AUTOPILOT_BRANCH}），fail-safe 已中止：{detail}；"
                    f"若部署環境缺 Administration:read 權限而持續卡此，設 "
                    f"TI_AUTOPILOT_PROTECTION_CHECK=0 跳過此檢查"
                )

        # 預設非強制推送（全新分支即可成功）；僅 FORCE_PUSH 開啟才用 --force-with-lease
        # 搭配 --force-if-includes（杜絕背景 fetch 讓 lease 退化成裸 force）。絕不用裸 -f。
        push_flags = (
            ["--force-with-lease", "--force-if-includes"] if config.AUTOPILOT_FORCE_PUSH else []
        )
        rc, push_url = await _run(
            ["git", "remote", "get-url", "--push", "origin"],
            cwd=clone,
            timeout=30,
        )
        if rc != 0:
            return False, f"無法確認 origin push URL，已中止：{push_url[-400:]}"
        if _repo_key(push_url) != repo_key:
            return False, (
                f"origin push URL 不等於 AUTOPILOT_REPO，已中止推送："
                f"origin={push_url.strip() or '(empty)'} autopilot={repo}"
            )
        rc, out = await _run(
            ["git", *_GIT_CRED, "push", *push_flags, "-u", "origin", branch],
            cwd=clone,
            timeout=180,
        )
        if rc != 0:
            return False, f"push 失敗：{out[-400:]}"
        body = f"autopilot 自動產生：{task['title']}\n\n{task.get('detail', '')}".strip()
        rc, out = await _run(
            [
                *_GH,
                "pr",
                "create",
                "-R",
                repo,
                "--base",
                config.AUTOPILOT_BRANCH,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=clone,
            timeout=120,
        )
        if rc != 0:
            return False, f"開 PR 失敗：{out[-400:]}"

        # 取 PR 編號供 publisher 協調器使用；取不到就明確失敗，絕不 fall-through 盲合（避免合到沒等 CI）。
        rc, out = await _run(
            [*_GH, "pr", "view", branch, "-R", repo, "--json", "number", "-q", ".number"],
            cwd=clone,
            timeout=60,
        )
        try:
            pr_number = int(out.strip())
        except (TypeError, ValueError):
            return False, f"無法取得 PR 編號（已開 PR 但解析失敗，未合併）：{out[-400:]}"

        # 等 CI 綠後才合併：複用 publisher 已測過的合併協調器（每輪「查狀態→等 CI→合併」，
        # behind/stale 會 _update_branch 後重試）。入口已把目標 repo 覆寫成 AUTOPILOT_REPO，
        # 避免 publisher REST 函式 fallback 到 config.PUBLISH_REPO（兩者未必相同）。
        outcome, detail = await publisher._merge_flow(
            pr_number,
            publisher.merge_payload(branch, "squash"),
            ci_timeout=config.PUBLISH_CI_TIMEOUT,
            ci_interval=config.PUBLISH_CI_INTERVAL,
            retries=config.PUBLISH_MERGE_RETRIES,
        )
        if outcome == publisher.MergeOutcome.MERGED:
            return MergeResult(
                True,
                f"已 squash-merge {branch} 進 {config.AUTOPILOT_BRANCH}（CI 綠後合併）：{detail}",
                pr_number=pr_number,
                branch=branch,
            )
        # 非綠/未合併：關閉 PR 並刪分支，避免留下孤兒 PR
        await _run(
            [*_GH, "pr", "close", "-R", repo, branch, "--delete-branch"],
            cwd=clone,
            timeout=120,
        )
        return (
            False,
            f"CI 未過或合併失敗（{outcome.value if hasattr(outcome, 'value') else outcome}）：{detail}",
        )
    finally:
        publisher.reset_repo_override(token)


# --- 並發協調 ------------------------------------------------------------


async def _wait_until_idle(timeout: int = 600) -> bool:
    """重佈會 restart ti.service，先等手動討論結束（無『真正進行中』的 session）。

    用 history.busy_sessions(stale 門檻) 而非裸 status==running，避免崩潰沒收尾、
    卡在 running 的死 session 讓守衛永久延後重佈。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        running = history.busy_sessions(config.DEPLOY_STALE_AFTER)
        if not running:
            return True
        log.info("有進行中的討論，延後重佈…(%d)", len(running))
        await asyncio.sleep(15)
    return False


# --- 自我評估 ------------------------------------------------------------


def _recent_done_titles() -> set[str]:
    """近期已完成任務的標題集合（依 AUTOPILOT_EVAL_MEMORY 取最新 N 筆），供去重過濾。"""
    return backlog.recent_done_titles(config.AUTOPILOT_EVAL_MEMORY)


def _sanitize_for_prompt(s: str, maxlen: int) -> str:
    """嵌入 prompt 前的統一消毒：壓平換行（\\r/\\n）、去頭尾空白、限長。

    單一真值來源——所有要拼進 discovery prompt 的 backlog 欄位（pending 標題、done/failed 標題、
    失敗 note）都過這道，阻斷標題/備註含 `\\n任務: …` 穿透 join 邊界、偽造任務行被下輪 parse 執行
    （prompt injection）。日後新增嵌入欄位沿用此 helper，避免漏網。
    """
    return (s or "").replace("\r", " ").replace("\n", " ").strip()[:maxlen]


def _recent_outcomes_context() -> str:
    """把迴圈自身的近期成敗（done/失敗＋失敗原因）整理成可注入評估提示的文字。

    純讀 backlog、無 LLM/網路，方便單測。AUTOPILOT_EVAL_MEMORY=0 或無成敗紀錄時回 ""，
    呼叫端據此維持原本的無狀態提示（零行為變更）。標題／note 在拼接前一律過
    `_sanitize_for_prompt`（與 `_pending_titles` 對稱），防 prompt injection。
    """
    limit = config.AUTOPILOT_EVAL_MEMORY
    if limit <= 0:
        return ""

    def _recent(status: str) -> list[dict]:
        rows = sorted(
            backlog.list_tasks(status), key=lambda t: t.get("updated_at", 0), reverse=True
        )
        return rows[:limit]

    done = _recent("done")
    failed = _recent("failed")
    if not done and not failed:
        return ""

    lines = ["【本工作室過往成績單（請據此提出全新、不重複的改善點）】"]
    if done:
        lines.append("✅ 近期已完成（請勿重複提出）：")
        lines += [f"- {_sanitize_for_prompt(t.get('title', ''), 200)}" for t in done]
    if failed:
        lines.append("❌ 近期失敗（除非有明確不同的新做法，否則勿重蹈覆轍）：")
        for t in failed:
            note = _sanitize_for_prompt(t.get("note") or "", 300)
            title = _sanitize_for_prompt(t.get("title", ""), 200)
            lines.append(f"- {title}" + (f" — {note}" if note else ""))
    return "\n".join(lines) + "\n\n"


def _pending_titles() -> list[str]:
    """目前仍在排隊／進行中的任務標題（pending + in_progress），供 prompt 注入與 pre-filter 對齊。

    純讀 backlog、無 LLM/網路。兩層防線（prompt 禁止清單、進場 pre-filter）的覆蓋範圍以此統一，
    避免「措辭滑溜的 in_progress 重複」漏網。標題在回傳前壓平換行並限長 200 字，作為嵌入 prompt
    前的明確防線（即使日後新增寫入 backlog 的路徑也不會讓多行標題穿透 prompt 結構）。
    """
    rows = [t for t in backlog.list_tasks() if t.get("status") in ("pending", "in_progress")]
    # 拼接前消毒：統一過 `_sanitize_for_prompt`（壓平換行、限長），阻斷標題穿透 prompt 結構。
    return [clean for t in rows if (clean := _sanitize_for_prompt(t.get("title", ""), 200))]


def north_star_context() -> str:
    """把長期目標（config.AUTOPILOT_NORTH_STAR）組成 discovery prompt 段。

    單一組裝點：autopilot 自評與 improver「找問題」皆由此取段，目標本身的單一真相在
    config（TI_AUTOPILOT_NORTH_STAR，可 reload）。目標為空時回 ""（該段自然消失）。
    嵌入前過 `_sanitize_for_prompt`，防多行值穿透 prompt 結構。
    """
    ns = _sanitize_for_prompt(config.AUTOPILOT_NORTH_STAR, 300)
    if not ns:
        return ""
    return f"【本工作室長期目標】{ns}。提案須可追溯到此目標。\n\n"


def _pending_awareness_context(titles: list[str] | None = None) -> str:
    """把目前 pending/in_progress 標題整理成 bullet 清單（只回資料，不含任何硬指令）。

    刻意只輸出「清單內容」、不內嵌指令：函式重用時不會把指令帶著走，且可單獨斷言清單內容。
    硬指令由 `_build_discovery_prompt` 在組裝層附加。無任何排隊任務時回 ""。
    """
    titles = _pending_titles() if titles is None else titles
    if not titles:
        return ""
    lines = ["【目前已在排隊／進行中的任務（請勿與下列任何項目實質重疊）】"]
    lines += [f"- {t}" for t in titles]
    return "\n".join(lines) + "\n\n"


def _oversubscribed_context(titles: list[str] | None = None, k: int | None = None) -> str:
    """把「pending 已過多的子系統」整理成提示段（只回資料＋一句指引）。

    複用 #3 的子系統抽取／計數邏輯（`_extract_subsystems`／`_count_subsystem_coverage`，定義於下方），
    達到門檻（同子系統計數 >= k，預設 config.AUTOPILOT_SUBSYSTEM_MAX）的子系統才列出；沒有任何子系統
    超標時回 ""（讓 prompt 不出現此段）。隨 pending 分佈動態變化，可單測斷言。

    與進場 pre-filter 的硬擋門檻 `AUTOPILOT_SUBSYSTEM_MAX_PENDING` 分層互補：此處是 prompt 軟引導
    （預設 2，早一步提醒 LLM 繞開），pre-filter 才在 `_MAX_PENDING`（預設 3）硬性拒收。
    """
    titles = _pending_titles() if titles is None else titles
    k = config.AUTOPILOT_SUBSYSTEM_MAX if k is None else k
    counts = _count_subsystem_coverage(titles)
    over = sorted(
        ((label, n) for label, n in counts.items() if n >= k),
        key=lambda x: (-x[1], x[0]),
    )
    if not over:
        return ""
    lines = ["【下列子系統的排隊任務已過多，請避免再對它們提案，改去覆蓋其他模組】"]
    lines += [f"- {label}（已有 {n} 筆）" for label, n in over]
    return "\n".join(lines) + "\n\n"


def _build_discovery_prompt(
    *,
    outcomes: str | None = None,
    pending: str | None = None,
    titles: list[str] | None = None,
) -> str:
    """組裝自我評估的 discovery prompt（純字串、無 LLM/網路，可單測）。

    結構：長期目標（北極星）+ 近期成敗回顧 + pending-awareness 清單 + 任務基底說明 + 兩條硬指令。
    兩條硬指令（禁止實質重疊、優先廣度覆蓋不同子系統）明確置於組裝層，為上層決策而非資料層職責。
    參數預設由 backlog 即時讀取；測試可注入字串以隔離 backlog 狀態。

    `titles` 為 pending/in_progress 標題快照的單一注入點：`pending` 與 `oversubscribed`
    兩段皆由它衍生（同源同快照），測試只需傳 `titles=` 即可隔離 backlog，無須 monkeypatch
    全域 `_pending_titles`。顯式傳入 `pending` 仍可單獨覆蓋該段（保留既有注入契約）。
    """
    titles = _pending_titles() if titles is None else titles
    outcomes = _recent_outcomes_context() if outcomes is None else outcomes
    pending = _pending_awareness_context(titles) if pending is None else pending
    # 「已過多子系統」段隨 pending 子系統分佈動態產生：有子系統超標才出現，否則為 ""。
    # 與 pending-awareness 同源（同一 titles 快照），確保 prompt 兩段覆蓋一致、且可注入隔離。
    oversubscribed = _oversubscribed_context(titles)
    # 措辭隨清單存否切換：有清單才講「上列」，避免空清單時硬指令 1 措辭懸空指向不存在的清單。
    rule_1 = (
        "1. 不得提出與上列任何已在排隊／進行中項目實質重疊者（同一主題換句話說也算重疊，一律避開）。\n"
        if pending
        else "1. 目前尚無排隊／進行中任務，但各點之間仍不得實質重疊（同一主題換句話說也算）。\n"
    )
    return (
        north_star_context()
        + outcomes
        + pending
        + oversubscribed
        + (
            "你正在審視「Ti Studio」這個 AI 多專家自主開發工作室專案本身（原始碼就在你的工作目錄）。\n"
            "請用 Read/Grep 快速瀏覽程式碼與測試，找出最值得改善的 1~5 點（真實 bug、缺測試、"
            "功能缺口、安全），每點獨立一行,格式固定為 `任務: <動詞開頭的具體任務>`。只輸出任務行。\n"
            "硬性要求：\n"
            + rule_1
            + "2. 優先廣度：每點須來自不同子系統，優先覆蓋近期未碰過的模組，禁止往同一主題反覆疊加。\n"
            + _DISCOVERY_QUALITY_BAR
        )
    )


# 低價值／陷阱型提案類型清單——discovery 兩條提案路徑（autopilot 自評 + improver「找問題」）
# 共用的**單一真相**。兩端都在 prompt 階段用它擋掉瑣碎任務，避免低價值提案進 backlog 跑完一輪才被
# 當噪音手動刪除（事後刪 → 源頭擋）。改這份清單即同時影響兩條路徑，勿在他處複製貼上分叉。
DISCOVERY_LOW_VALUE_TYPES = (
    "   - 純文件／格式微調（python→python3、docstring 換行對齊、移除暫存檔、補標題前綴）；\n"
    "   - 對既有防線／守門『稽核確認是否到位』而無具體已知 bug 的自我審查；\n"
    "   - 純流程結構任務（補 AST guard、加交付 git status 守門、把字面斷言改關鍵字、收斂 deprecation warning）；\n"
    "   - 『確認某檔該不該留／盤點追蹤狀態』這類純調查；\n"
    "   - 對已有上限／截斷的模組再疊加一層防禦。"
)

# 第三道硬指令：品質下限。autopilot 長跑時 discovery 會反覆自我餵食「稽核既有防線是否到位」
# 「文件 python→python3」「補交付守門/AST guard」「確認某檔該不該留」這類低價值/陷阱型提案，
# 跑了燒額度、產出多是噪音。明確列為禁止輸出類型，並要求「寧缺勿濫」——湊不滿就少給，不得充數。
_DISCOVERY_QUALITY_BAR = (
    "3. 品質下限：只提『使用者或開發者可感知的具體缺陷或功能缺口』，每點須能指出證據"
    "（檔案:行號＋症狀或重現）。以下低價值類型一律不要輸出；高價值點不足時寧可只給 1~2 點，"
    "嚴禁用這類充數：\n" + DISCOVERY_LOW_VALUE_TYPES
)


# 同義詞 canonical 正規化：**單一常數表**，分兩道 pass 的子映射（架構定案：分層替換，不用扁平
# str.replace——`fix`/`add` 會誤命中 `prefix`/`address`，是設計表裡就收的詞，必須以分層精確匹配避開）。
#   - `"cjk"`（Pass 1，字串級，CJK 多字詞 → ASCII canonical）：
#       字串級替換「無邊界保護」（ASCII `\b` 對 CJK 無效），故 key 一律選 **≥2 字、辨識度高** 的 CJK 詞，
#       單字（如「改」）絕對不收——避免命中同義前綴造成誤殺。替換時按 key 長度降冪（長詞優先）。
#   - `"ascii"`（Pass 2，token 級，ASCII token → ASCII canonical）：
#       在切完 token 後逐 token 做精確 `dict.get`，token 已是完整片段，零子字串風險。
# 邊界：僅收斂「已知會重複出現」的少數主題；**不窮舉同義、不引 embedding**。known-limitation：
#   無共享字且不在此表的同義改寫（如某些「補↔新增」變體）仍可能從第一道漏網，由第二道廣度防線兜底。
_SYNONYM_CANONICAL: dict[str, dict[str, str]] = {
    "cjk": {
        "去重": "dedup",
        "deduplication": "dedup",
        "修復": "fix",
        "修正": "fix",
        "新增": "add",
        "補上": "add",
        "改良": "improve",
        "改善": "improve",
        "優化": "improve",
    },
    "ascii": {
        # 註：不收 "deduplication"——Pass 1（字串級）已先把它消化成 dedup，token 永不殘留此鍵。
        "dedupe": "dedup",
        "fixes": "fix",
        "fixing": "fix",
        "adds": "add",
        "adding": "add",
        "improves": "improve",
        "improving": "improve",
        "improvement": "improve",
        "optimize": "improve",
        "optimise": "improve",
    },
}

# Pass 1 替換順序：長詞優先（降冪），避免短同義詞先截斷長詞。模組載入時固定一次。
_SYNONYM_CJK_ORDERED: list[tuple[str, str]] = sorted(
    _SYNONYM_CANONICAL["cjk"].items(), key=lambda kv: len(kv[0]), reverse=True
)


def _normalize_for_dedup(s: str) -> str:
    """相似度比對前的正規化：壓平換行、strip、轉小寫、去首尾標點，並做同義詞 Pass 1 展開。

    供 `_tokenize_for_dedup` 前置使用；獨立成 helper 方便測試與日後替換策略。

    Pass 1（CJK 多字詞 → ASCII canonical）：在逐字切 token 前，先把 `_SYNONYM_CANONICAL["cjk"]`
    的 CJK 同義詞（去重→dedup、修復/修正→fix…）展開成空白包夾的 ASCII canonical，使「無共享字的
    同義改寫」（如「修復去重」↔「修正 dedup 邏輯」）在詞集層面對齊、被第一道相似度攔下。
    替換前後補空白避免與相鄰 CJK 黏連成單一 token。

    known-limitation：此表為 **小型、不窮舉** 的 canonical 正規化，僅收斂已知重複主題；
    未收錄的同義改寫仍可能漏網，刻意 **不引入 embedding／語意向量**，由第二道子系統廣度防線兜底。
    """
    s = s.replace("\n", " ").strip().lower()
    s = s.strip(" 　\t.,;:!?。，、；：！？「」『』()（）[]【】-_\"'")
    for syn, canon in _SYNONYM_CJK_ORDERED:
        if syn in s:
            s = s.replace(syn, f" {canon} ")
    return s


# 子系統關鍵詞 → 從標題抽「涉及的子系統」，供「同子系統 pending 過多即拒」的廣度防線（第二道）使用。
# 邊界策略（動工前已實測釘住）：
#   - 英文/latin 詞一律加 `\b` 邊界，避免 `ci`→`social`、`merge`→`emergence`、`decide` 等子詞誤命中。
#   - CJK 詞（去重/評估）用「純子字串」而非 lookahead/`\b`：實測 `(?<!\w)去重(?!\w)` 與
#     `(?<![^\s，。！？])去重(?![^\s，。！？])` 在連續中文標題（如「改善去重邏輯效能」「強化提案去重」）
#     **完全不命中**——CJK 周邊無空白/標點邊界，邊界寫法反而漏抓真正想攔的子系統。去重/評估皆為具
#     辨識度的多字詞，子字串誤命中風險極低，故對 CJK 不加邊界。匹配一律 re.IGNORECASE。
# 同一標題可命中多個子系統（各記一次）；下方統一以正規名收斂單複數變體（experts↔expert…）。
_SUBSYSTEM_PATTERNS: list[tuple[str, str]] = [
    ("backlog", r"\bbacklog\b"),
    ("discovery", r"\bdiscovery\b"),
    ("autopilot", r"\bautopilot\b"),
    ("experts", r"\bexperts?\b"),
    ("providers", r"\bproviders?\b"),
    ("runner", r"\brunner\b"),
    ("orchestrator", r"\borchestrat\w*\b"),
    ("secure_write", r"\bsecure[\s_-]?write\b"),
    ("branch_protect", r"\bbranch[\s_-]?protect\w*\b"),
    ("ci", r"\bci\b"),
    ("merge", r"\bmerge\b"),
    ("去重", r"去重"),
    ("評估", r"評估"),
]
_SUBSYSTEM_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pat, re.IGNORECASE)) for name, pat in _SUBSYSTEM_PATTERNS
]


def _extract_subsystems(title: str) -> set[str]:
    """從單一標題抽出涉及的子系統正規名集合（無命中則空集合）。匹配固定套 re.IGNORECASE。"""
    return {name for name, rx in _SUBSYSTEM_COMPILED if rx.search(title)}


def _count_subsystem_coverage(titles: list[str]) -> Counter[str]:
    """統計一批標題在各子系統的覆蓋筆數，回傳 collections.Counter[str]（支援 `[k] >= K` 比較）。

    一個標題若同時命中多個子系統，對每個子系統各計一次（廣度判斷以「涉及」為準，非互斥分類）。
    """
    cov: Counter[str] = Counter()
    for t in titles:
        for s in _extract_subsystems(t):
            cov[s] += 1
    return cov


# CJK 統一表意文字（含擴展 A）區段——逐字當作一個 token，不依賴外部分詞器（不引入 jieba）。
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")
# ASCII 英數連續片段（如 backlog、ci、retry）整段當作一個 token，大小寫已由 normalize 壓平。
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize_for_dedup(s: str) -> set[str]:
    """把標題切成「詞集」：ASCII 英數片段整段成 token、CJK 逐字成 token，丟標點空白。

    純 stdlib（re），不引入分詞依賴。CJK 逐字是刻意取捨：同義改寫常共享字根（如「補測試」/
    「新增測試」共享「測試」），逐字交集比字元序列比對更穩；代價是字級分詞無法辨識「補↔新增」
    這類無共享字的同義替換（見 known-limitation 測試）。
    """
    s = _normalize_for_dedup(s)
    # Pass 2：ASCII token 精確映射到 canonical（dict.get 精確匹配，零子字串汙染）。
    ascii_map = _SYNONYM_CANONICAL["ascii"]
    ascii_toks = {ascii_map.get(t, t) for t in _ASCII_TOKEN_RE.findall(s)}
    toks = ascii_toks
    toks.update(_CJK_RE.findall(s))
    return toks


def _token_set_similarity(a: str, b: str) -> float:
    """詞集 Jaccard 相似度：|A∩B| / |A∪B|，任一為空回 0.0。

    取代舊的 `difflib.SequenceMatcher`（字元序列比對）。Jaccard 是集合運算、與語序無關，
    因此能抓到舊策略漏掉的「語序調換」改寫（如「為 retry 機制加上重試上限」↔
    「為重試機制加上 retry 上限」：SequenceMatcher≈0.625 漏網，Jaccard=1.0 攔下）。
    """
    ta, tb = _tokenize_for_dedup(a), _tokenize_for_dedup(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _filter_pending_duplicates(proposals: list[str], existing_titles: list[str]) -> list[str]:
    """進場 pre-filter：兩道互補防線，皆只作用於本次提案進場，皆不回溯刪改 backlog、不動
    `backlog._is_duplicate` 的字串等值去重契約。第一道相似度用 `_token_set_similarity`
    （詞集 Jaccard，取代舊字元序列比對）以捕中文同義改寫與語序調換。

    第一道（相似度）：丟掉與任一 existing 標題相似度 ≥ `AUTOPILOT_DEDUP_RATIO` 者（擋換句話說的重複）。
    第二道（子系統覆蓋廣度）：以 regex 從標題抽「涉及子系統」，若某子系統在 existing 已達
        `AUTOPILOT_SUBSYSTEM_MAX_PENDING`(K) 筆，該子系統的新提案一律拒——擋 LLM 不換標題卻反覆對同一
        模組疊加（topic echo chamber）。已通過第一道的提案，其子系統計入 running count，避免同一批提案
        一次塞爆同一子系統。

    比對/計數範圍與 `_pending_awareness_context` 注入 prompt 的禁止清單對齊（pending + in_progress）。
    # O(n×m)，其中 n=proposals 數、m=existing 數；existing 預期 < 50 筆，若規模增長需重估。
    """
    if not existing_titles:
        return proposals
    kept: list[str] = []
    for p in proposals:
        hit = next(
            (
                e
                for e in existing_titles
                if _token_set_similarity(p, e) >= config.AUTOPILOT_DEDUP_RATIO
            ),
            None,
        )
        if hit is not None:
            log.debug("pre-filter 丟棄與排隊任務高相似的提案：%r（近似 %r）", p, hit)
            continue
        kept.append(p)
    # 第二道：子系統覆蓋廣度防線。coverage 以 existing 為基底，接受的提案逐筆累加進去。
    coverage = _count_subsystem_coverage(existing_titles)
    k = config.AUTOPILOT_SUBSYSTEM_MAX_PENDING
    final: list[str] = []
    for p in kept:
        subs = _extract_subsystems(p)
        crowded = sorted(s for s in subs if coverage[s] >= k)
        if crowded:
            log.debug("pre-filter 丟棄子系統已過多(≥%d)的提案：%r（子系統 %s）", k, p, crowded)
            continue
        for s in subs:
            coverage[s] += 1
        final.append(p)
    return final


async def _evaluate_self(clone: str) -> int:
    """backlog 空時，用一位資深專家審視 Ti 自身並產出改善任務。回傳新增數。

    會先把迴圈自身的近期成敗回饋給專家（self-reinforcing）：避免重提已完成、避開已知失敗做法。
    並注入目前 pending/in_progress 標題（pending-awareness）＋兩條硬指令（禁止實質重疊、優先廣度
    覆蓋不同子系統），讓專家在產出階段就迴避與排隊任務重疊。prompt 組裝抽到 `_build_discovery_prompt`
    以利單測。

    專家產出後再過兩道進場過濾，才交給 `backlog.add_many`：
      1. 丟掉與近期已完成標題完全相符者（`_recent_done_titles`），補 backlog 去重對 done 的缺口。
      2. 進場 pre-filter（`_filter_pending_duplicates`）：丟掉與 pending/in_progress 標題語意相近
         （詞集 Jaccard `_token_set_similarity` ≥ `AUTOPILOT_DEDUP_RATIO`）者，與 prompt 注入的禁止清單範圍對齊。
    兩道過濾僅作用於本次提案進場，皆不改動 `backlog._is_duplicate` 的字串等值去重契約，與其互補。
    """
    from .experts import Expert
    from .roles import SENIOR

    sid = f"ap-eval-{uuid.uuid4().hex[:8]}"
    ex = Expert(SENIOR, sid, Path(clone))

    async def _noop(_ev):
        return None

    # 取一次 pending/in_progress 快照，prompt 注入與進場 pre-filter 共用同一份，
    # 杜絕 LLM 延遲期間 backlog 變動造成兩端快照分裂（prompt 引導與 filter 比對不一致）。
    titles = _pending_titles()
    prompt = _build_discovery_prompt(titles=titles)
    try:
        text = await ex.speak(prompt, _noop)
    finally:
        with contextlib.suppress(Exception):
            await ex.stop()
    # 過濾掉與近期已完成標題完全相符者（補 backlog 去重對 done 的缺口，避免剛完成又重排）。
    raw = parse_tasks(text)
    done_titles = _recent_done_titles()
    tasks = [t for t in raw if t.strip() not in done_titles]
    # 進場 pre-filter：丟掉與目前 pending/in_progress 高相似（語意相近）的提案，與 prompt 注入的
    # 禁止清單對齊（同一 titles 快照）。不動 backlog._is_duplicate 的字串等值去重契約，兩者互補。
    tasks = _filter_pending_duplicates(tasks, titles)
    n = backlog.add_many(tasks, source="eval")
    # 留痕：兩道進場過濾（done 去重 + pending pre-filter）共丟棄多少提案——讓「源頭擋掉多少瑣碎/重複」
    # 可觀測，而非無聲 log.debug 消失（與 improver._discover 的丟棄留痕對齊）。
    log.info("自我評估產出 %d 個新任務（提案 %d、過濾丟棄 %d）", n, len(raw), len(raw) - len(tasks))
    return n


# --- 單一任務 ------------------------------------------------------------


def _handle_gate_failure(task: dict, gate_label: str, detail: str) -> None:
    """客觀閘門失敗時的收斂處置：有限次「重試同一任務」，用完才放棄。

    取代舊行為（每次失敗就 `backlog.add("修復X失敗…")` spawn 一個措辭近似的新任務，
    導致該任務再失敗又 spawn、backlog 無限暴增）。改為：
      - 還有重試額度：把同一任務退回 pending、attempts +1、附上失敗筆記，下輪重跑同一任務。
      - 額度用罄：標 failed 並註明放棄；不再 spawn 任何「修復X」新任務。

    注意只處置「閘門失敗的重試」；討論發現的新工作（followup_items／route_core_changes）
    仍走各自既有路徑，與此無關。
    """
    attempts = int(task.get("attempts") or 0)
    if attempts + 1 < config.AUTOPILOT_TASK_MAX_ATTEMPTS:
        backlog.set_status(
            task["id"],
            "pending",
            attempts=attempts + 1,
            note=f"[{gate_label}] 第 {attempts + 1} 次未過，重試；{detail[-300:]}".strip(),
        )
        log.info(
            "任務 #%s %s 未過，退回 pending 重試（第 %d 次）", task["id"], gate_label, attempts + 1
        )
    else:
        backlog.set_status(
            task["id"],
            "failed",
            note=f"[{gate_label}] 連續 {config.AUTOPILOT_TASK_MAX_ATTEMPTS} 次未過，放棄；{detail[-300:]}".strip(),
        )
        log.info(
            "任務 #%s %s 連續 %d 次未過，標 failed 放棄",
            task["id"],
            gate_label,
            config.AUTOPILOT_TASK_MAX_ATTEMPTS,
        )


async def run_one_task(task: dict) -> None:
    sid = f"ap{uuid.uuid4().hex[:10]}"
    backlog.set_status(task["id"], "in_progress", session_id=sid)
    log.info("開始任務 #%s：%s（session %s）", task["id"], task["title"], sid)

    clone = await _prepare_clone()
    requirement = task["title"] + (f"\n\n細節：{task['detail']}" if task.get("detail") else "")

    history.start_session(sid, f"[autopilot] {task['title']}")

    async def broadcast(event):
        history.record_event(sid, event.to_dict())

    session = StudioSession(
        sid,
        broadcast,
        cwd=Path(clone),
        repo_url=f"https://github.com/{config.AUTOPILOT_REPO}",
        # 軟性時間預算＝硬 timeout：session 會在其 SESSION_SOFT_DEADLINE_FRAC 比例處主動收斂、
        # 優雅出貨已完成成果，避免撞 wait_for 硬砍把整場(含已完成任務)全丟成 timeout failed。
        time_budget_s=config.AUTOPILOT_TASK_TIMEOUT or None,
        # session 不自行發佈：autopilot 作為唯一發佈者（_commit_push_merge 等 CI→合併），
        # 否則同一份成果會被 session（ti-studio/<sid>）與 autopilot（autopilot/task-N）各開一個 PR。
        auto_publish=False,
    )
    try:
        result = await asyncio.wait_for(
            session.run(requirement),
            timeout=config.AUTOPILOT_TASK_TIMEOUT or None,
        )
    except TimeoutError as exc:
        raise TimeoutError(
            f"autopilot task timeout after {config.AUTOPILOT_TASK_TIMEOUT}s"
        ) from exc
    finally:
        history.finish_session(sid)

    # 回饋：討論發現的後續任務寫回 backlog（優先含 priority/type 的結構化版本）
    if result.get("followup_items"):
        added = backlog.add_items(result["followup_items"], source="discovered")
        log.info("從討論新增 %d 個後續任務", added)
    elif result.get("followups"):
        added = backlog.add_many(result["followups"], source="discovered")
        log.info("從討論新增 %d 個後續任務", added)
    # autopilot 的 working clone 本身就是核心 repo（config.CORE_REPO），判定的核心改動經同一個
    # 收斂點路由（與 improver/ws 共用，含近期完成去重，避免做完又重排），以 source="core" 標記。
    core_added = backlog.route_core_changes(result.get("core_changes") or [])
    if core_added:
        log.info("從討論新增 %d 個核心改動", core_added)

    if result.get("provider_unavailable"):
        provider = str(result["provider_unavailable"])
        # 只把任務退回 pending 即 return，不再 _pause() 寫 pause 檔永久暫停等人工 resume：
        # 下一輪主迴圈的額度閘門（provider_quota.gate）會查快照，全受限就睡到最早重置再
        # 自動續跑，長跑不間斷。_pause() 保留給「重佈失敗」這類必須人工檢視的分支。
        backlog.set_status(task["id"], "pending", note=f"{provider} provider unavailable")
        log.warning(
            "任務 #%s 因 %s provider 不可用退回 pending，待額度閘門判定恢復後自動重跑",
            task["id"],
            provider,
        )
        return

    # 「完整完成」與「可帶已知限制出貨」分流：尾票（N-1/N 已過、單一子任務 known-limit）不該把
    # 整場判「討論未達完成」。shippable＝核心客觀證據已過、未過子任務已回填 backlog followup（上方
    # 已寫入）→ 不在此硬判 failed,改續走 lint/collect/test/merge 客觀閘門:真紅點由閘門擋並補修復
    # 任務,通過則以已知限制版本合併。完全不可出貨(沒跑過 Demo/被中止)才維持 failed。
    shipped_with_limits = False
    if not result.get("completed"):
        if not result.get("shippable"):
            backlog.set_status(task["id"], "failed", note="討論未達完成")
            log.info("任務 #%s 未完成且不可出貨,標 failed", task["id"])
            return
        shipped_with_limits = True
        log.info("任務 #%s 帶已知限制出貨,續走客觀閘門", task["id"])

    # 閘門 1：lint（對齊 CI lint job）—— ruff check + format
    ok, out = await _gate_lint(clone)
    if not ok:
        _handle_gate_failure(task, "lint", out)
        return

    # 閘門 2：無 SDK collection（對齊 CI test job 環境）
    ok, out = await _gate_collect_without_sdk(clone)
    if not ok:
        _handle_gate_failure(task, "collect", out)
        return

    # 閘門 3：完整測試必須全綠
    ok, out = await _gate_tests(clone)
    if not ok:
        _handle_gate_failure(task, "test", out)
        return

    # commit / push / squash-merge 進 main
    merge_res = await _commit_push_merge(clone, task)
    merged, msg = merge_res
    if not merged:
        _handle_gate_failure(task, "merge", msg)
        return
    log.info("任務 #%s %s", task["id"], msg)
    # PR 追溯欄位：成功路徑為 MergeResult（帶 pr_number/branch）；dryrun 等純 tuple 以
    # getattr 容錯取 None/""，不改變既有行為。
    done_fields = {
        "pr": getattr(merge_res, "pr_number", None),
        "merged_branch": getattr(merge_res, "branch", ""),
    }

    # 重佈（等手動討論結束才動,避免打斷使用者；deploy 會 fetch 最新 main,延後也會追上）
    if await _wait_until_idle():
        ok, dmsg = await deploy.redeploy()
        log.info("重佈：%s", dmsg)
        done_fields["deploy_msg"] = dmsg  # 含 old→new commit（deploy.redeploy 成功訊息）
        if not ok:
            backlog.set_status(task["id"], "failed", note=dmsg, **done_fields)
            backlog.add("修復導致重佈失敗的 regression", detail=dmsg, source="discovered")
            _pause("重佈失敗已自動回滾,暫停待人工檢視")
            return
    else:
        log.info("等待逾時,本輪略過重佈(下次任務會追上最新 main)")

    if shipped_with_limits:
        backlog.set_status(
            task["id"], "done", note="帶已知限制完成(部分子任務已回填 backlog)", **done_fields
        )
    else:
        backlog.set_status(task["id"], "done", **done_fields)
    log.info("任務 #%s %s", task["id"], "帶已知限制完成" if shipped_with_limits else "完成")


def _pause(reason: str) -> None:
    with contextlib.suppress(OSError):
        config.AUTOPILOT_PAUSE_FILE.write_text(f"{reason}\n{time.ctime()}\n", encoding="utf-8")
    log.warning("已暫停 autopilot：%s", reason)


def _recover_stale_in_progress() -> None:
    """把沒有活躍 history session 的 in_progress 任務放回 pending。

    autopilot 被 kill、LLM turn 被外部中止、或舊版流程卡在 session.run() 時，backlog 可能
    永久停在 in_progress。busy_sessions 已用 events mtime 做 stale 判定；這裡只負責把
    backlog 狀態拉回可重跑，避免主迴圈永遠看不到這筆任務。
    """
    busy = {m.get("session_id") for m in history.busy_sessions(config.DEPLOY_STALE_AFTER)}
    for task in backlog.list_tasks("in_progress"):
        sid = task.get("session_id")
        if sid in busy:
            continue
        if sid:
            history.mark_interrupted(sid, "autopilot stale in_progress recovery")
        backlog.set_status(
            task["id"],
            "pending",
            session_id=sid,
            note="autopilot stale in_progress recovery",
        )
        log.warning("回收 stale in_progress 任務 #%s（session %s）", task["id"], sid)


# --- 心跳 ----------------------------------------------------------------


def _quota_summary(snap: dict) -> dict[str, float | None]:
    """把額度快照壓成 ``{provider_key: max_used%}``，供心跳檔與 log 使用（None＝無用量資訊）。"""
    return {
        str(entry.get("key")): provider_quota._usage(entry)["max_used"]
        for entry in snap.get("providers", [])
    }


def _write_status(
    state: str,
    *,
    task_id: int | str | None = None,
    sleep_until: float | None = None,
    quota: dict | None = None,
) -> None:
    """心跳：每輪主迴圈把當前狀態原子寫入 ``<AUTOPILOT_STATE_DIR>/status.json``。

    state ∈ {"idle", "running", "quota_sleep"}；/api/autopilot 讀此檔回報「迴圈還活著、
    正在做什麼、睡到何時、各 provider 用量」。寫入走 secure_write_root（與 backlog 同範式，
    原子 tmp+rename）；心跳只是輔助觀測，任何寫入失敗都不得弄死主迴圈（僅留 debug log）。
    """
    payload = {
        "state": state,
        "task_id": task_id,
        "sleep_until": sleep_until,
        "updated_at": time.time(),
        "quota": quota or {},
    }
    try:
        config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        secure_write_root(
            config.AUTOPILOT_STATE_DIR / "status.json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 — 心跳失敗不影響主迴圈
        log.debug("心跳寫入 status.json 失敗（忽略，不影響主迴圈）", exc_info=True)


# --- 主迴圈 --------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("autopilot 啟動（dryrun=%s, repo=%s）", config.AUTOPILOT_DRYRUN, config.AUTOPILOT_REPO)
    startup_sig = _self_sig()

    while True:
        if config.autopilot_paused():
            await asyncio.sleep(10)
            continue

        # 額度閘門：取任務前先確認至少一個 provider 還有額度；全受限就睡到最早重置
        # （夾在 [60, AUTOPILOT_QUOTA_MAX_SLEEP]）後 continue 重查，避免額度耗盡時
        # 空轉把任務全燒成 failed。snapshot() 含阻塞 I/O，丟 to_thread 跑。
        quota: dict[str, float | None] = {}
        if config.AUTOPILOT_QUOTA_GATE:
            snap = await asyncio.to_thread(provider_quota.snapshot)
            quota = _quota_summary(snap)
            usable, reset_at = provider_quota.gate(snap)
            if not usable:
                now = time.time()
                wait = (reset_at - now) if reset_at is not None else 60.0
                sleep_s = min(max(wait, 60.0), float(config.AUTOPILOT_QUOTA_MAX_SLEEP))
                _write_status("quota_sleep", sleep_until=now + sleep_s, quota=quota)
                log.info(
                    "所有 provider 額度受限（%s），休眠 %.0f 秒後重查額度",
                    quota,
                    sleep_s,
                )
                await asyncio.sleep(sleep_s)
                continue

        _recover_stale_in_progress()
        task = backlog.next_pending()
        if task is None:
            _write_status("idle", quota=quota)
            clone = await _prepare_clone()
            n = await _evaluate_self(clone)
            if n == 0:
                log.info("backlog 空且無新任務,休息…")
                await asyncio.sleep(max(config.AUTOPILOT_COOLDOWN, 60))
            continue

        _write_status("running", task_id=task.get("id"), quota=quota)
        try:
            await run_one_task(task)
        except Exception as exc:  # noqa: BLE001 — 單一任務出錯不該弄死整個迴圈
            log.exception("任務 #%s 例外", task.get("id"))
            backlog.set_status(task["id"], "failed", note=f"{type(exc).__name__}: {exc}")

        # 部署後若自身程式碼有更新 → 重載自己,避免跑舊邏輯
        if not config.AUTOPILOT_DRYRUN and _self_sig() != startup_sig:
            log.info("偵測到 autopilot 自身程式碼更新,os.execv 重載")
            os.execv(sys.executable, [sys.executable, "-m", "studio.autopilot"])

        await asyncio.sleep(config.AUTOPILOT_COOLDOWN)


if __name__ == "__main__":
    asyncio.run(main())
