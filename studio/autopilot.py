"""Autopilot —— Ti Studio 的自主自我改善迴圈（獨立程序：`python -m studio.autopilot`）。

迴圈：取 backlog pending 任務 → 在 working clone 跑 headless 討論（專家在沙箱內改 Ti
自己）→ 跑完整 pytest 當閘門 → 綠才 commit/push/squash-merge 進 main → 重佈 ti.service
（含健康檢查+自動回滾）→ 把討論發現的後續任務寫回 backlog → 下一個。backlog 空時跑
自我評估產生新任務。可隨時用暫停開關（pause 檔）叫停；改到自身程式碼後 os.execv 重載。

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

from . import backlog, config, deploy, history, runner
from .orchestrator import StudioSession, parse_tasks

log = logging.getLogger("ti.autopilot")

_GH = ["gh"]
_GIT_CRED = ["-c", "credential.helper=!gh auth git-credential"]
# 改到這些檔（影響迴圈自身行為）就 os.execv 重載。
_SELF_FILES = ("autopilot.py", "deploy.py", "backlog.py", "config.py")


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
    """autopilot 自身關鍵檔的最新 mtime 總和，用來判斷部署後是否需重載自己。"""
    base = Path(__file__).resolve().parent
    total = 0.0
    for name in _SELF_FILES:
        with contextlib.suppress(OSError):
            total += (base / name).stat().st_mtime
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


async def _commit_push_merge(clone: str, task: dict) -> tuple[bool, str]:
    """把成果開分支、push、squash-merge 進 main。dryrun 只回報。"""
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
        ["git", *_GIT_CRED, "ls-remote", "--heads", "origin", branch], cwd=clone, timeout=60
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
    # unprotected 皆放行（受保護分支由既有不帶 --admin 的 pr merge 自然攔下，不重複把關）。
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
    rc, out = await _run(
        ["git", *_GIT_CRED, "push", *push_flags, "-u", "origin", branch], cwd=clone, timeout=180
    )
    if rc != 0:
        return False, f"push 失敗：{out[-400:]}"
    repo = config.AUTOPILOT_REPO
    body = f"autopilot 自動產生：{task['title']}\n\n{task.get('detail', '')}".strip()
    await _run(
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
    # 預設不帶 --admin，讓 GitHub 分支保護/必過檢查生效；僅 MERGE_ADMIN 為真才繞過保護。
    admin_flag = ["--admin"] if config.AUTOPILOT_MERGE_ADMIN else []
    rc, out = await _run(
        [*_GH, "pr", "merge", "-R", repo, branch, "--squash", *admin_flag, "--delete-branch"],
        cwd=clone,
        timeout=180,
    )
    if rc != 0:
        return False, f"merge 失敗：{out[-400:]}"
    return True, f"已 squash-merge {branch} 進 {config.AUTOPILOT_BRANCH}"


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

    結構：近期成敗回顧 + pending-awareness 清單 + 任務基底說明 + 兩條硬指令。
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
        outcomes
        + pending
        + oversubscribed
        + (
            "你正在審視「Ti Studio」這個 AI 多專家自主開發工作室專案本身（原始碼就在你的工作目錄）。\n"
            "請用 Read/Grep 快速瀏覽程式碼與測試，找出最值得改善的 3~5 點（bug、缺測試、可讀性、"
            "功能缺口、安全），每點獨立一行,格式固定為 `任務: <動詞開頭的具體任務>`。只輸出任務行。\n"
            "硬性要求：\n"
            + rule_1
            + "2. 優先廣度：每點須來自不同子系統，優先覆蓋近期未碰過的模組，禁止往同一主題反覆疊加。"
        )
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
    done_titles = _recent_done_titles()
    tasks = [t for t in parse_tasks(text) if t.strip() not in done_titles]
    # 進場 pre-filter：丟掉與目前 pending/in_progress 高相似（語意相近）的提案，與 prompt 注入的
    # 禁止清單對齊（同一 titles 快照）。不動 backlog._is_duplicate 的字串等值去重契約，兩者互補。
    tasks = _filter_pending_duplicates(tasks, titles)
    n = backlog.add_many(tasks, source="eval")
    log.info("自我評估產出 %d 個新任務", n)
    return n


# --- 單一任務 ------------------------------------------------------------


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
        backlog.set_status(task["id"], "pending", note=f"{provider} provider unavailable")
        _pause(f"{provider} provider unavailable")
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
        backlog.set_status(task["id"], "failed", note="[lint] 閘門未通過")
        backlog.add(f"修復 lint 失敗：{task['title']}", detail=out[-500:], source="discovered")
        log.info("任務 #%s lint 未過,標 failed 並補修復任務", task["id"])
        return

    # 閘門 2：無 SDK collection（對齊 CI test job 環境）
    ok, out = await _gate_collect_without_sdk(clone)
    if not ok:
        backlog.set_status(task["id"], "failed", note="[collect] 無 SDK collection 失敗")
        backlog.add(
            f"修復缺 SDK collection：{task['title']}", detail=out[-500:], source="discovered"
        )
        log.info("任務 #%s 無 SDK collection 失敗,標 failed 並補修復任務", task["id"])
        return

    # 閘門 3：完整測試必須全綠
    ok, out = await _gate_tests(clone)
    if not ok:
        backlog.set_status(task["id"], "failed", note="[test] 測試未通過")
        backlog.add(f"修復測試失敗：{task['title']}", detail=out[-500:], source="discovered")
        log.info("任務 #%s 測試未過,標 failed 並補修復任務", task["id"])
        return

    # commit / push / squash-merge 進 main
    merged, msg = await _commit_push_merge(clone, task)
    if not merged:
        backlog.set_status(task["id"], "failed", note=msg)
        log.info("任務 #%s 未合併：%s", task["id"], msg)
        return
    log.info("任務 #%s %s", task["id"], msg)

    # 重佈（等手動討論結束才動,避免打斷使用者；deploy 會 fetch 最新 main,延後也會追上）
    if await _wait_until_idle():
        ok, dmsg = await deploy.redeploy()
        log.info("重佈：%s", dmsg)
        if not ok:
            backlog.set_status(task["id"], "failed", note=dmsg)
            backlog.add("修復導致重佈失敗的 regression", detail=dmsg, source="discovered")
            _pause("重佈失敗已自動回滾,暫停待人工檢視")
            return
    else:
        log.info("等待逾時,本輪略過重佈(下次任務會追上最新 main)")

    if shipped_with_limits:
        backlog.set_status(task["id"], "done", note="帶已知限制完成(部分子任務已回填 backlog)")
    else:
        backlog.set_status(task["id"], "done")
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


# --- 主迴圈 --------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("autopilot 啟動（dryrun=%s, repo=%s）", config.AUTOPILOT_DRYRUN, config.AUTOPILOT_REPO)
    startup_sig = _self_sig()

    while True:
        if config.autopilot_paused():
            await asyncio.sleep(10)
            continue

        _recover_stale_in_progress()
        task = backlog.next_pending()
        if task is None:
            clone = await _prepare_clone()
            n = await _evaluate_self(clone)
            if n == 0:
                log.info("backlog 空且無新任務,休息…")
                await asyncio.sleep(max(config.AUTOPILOT_COOLDOWN, 60))
            continue

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
