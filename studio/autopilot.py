"""Autopilot —— Ti Studio 的自主自我改善迴圈（獨立程序：`python -m studio.autopilot`）。

迴圈：取 backlog pending 任務 → 在 working clone 跑 headless 討論（專家在沙箱內改 Ti
自己）→ 跑完整 pytest 當閘門 → 綠才 commit/push/squash-merge 進 main → 重佈 ti.service
（含健康檢查+自動回滾）→ 把討論發現的後續任務寫回 backlog → 下一個。backlog 空時跑
自我評估產生新任務。可隨時用暫停開關（pause 檔）叫停；改到自身程式碼後 os.execv 重載。

長跑不間斷：迴圈頂端有額度閘門（provider_quota.gate）——全部 provider 額度受限時睡到
最早重置再重查，而非空轉燒失敗；provider 中途不可用只把任務退回 pending（不寫 pause 檔）。
Claude 訂閱雙帳號會自動分配（決策在 claude_accounts.pick_account，v4 優先序：95% 安全上限
> 7d 早重置多吃（差 ≥ reset_edge_7d；7d 窗是週尺度稀缺資源，早歸還的先吃掉才不浪費）
> 5h 早重置多吃（差 ≥ reset_edge；日內節奏）> 負載平均分配（負載＝5h/7d 兩窗取最大，
差 ≥ margin 即切換攤平），全部達上限交給 quota gate），切換後排程重啟服務使新憑證生效。
每輪把 {state, task_id, sleep_until, quota…} 心跳寫進 <state dir>/status.json 供 /api/autopilot 觀測；
任務執行中工具/發言事件會刷新 last_activity_at 與目前專家 turn，另有背景任務每分鐘保底刷新
updated_at＋last_activity_at＋workers.cpu_active（後者以子行程 CPU 取樣補足 events mtime 在長
inter-message 間隔會凍結的盲區），長任務不再被外部監控誤判死鎖。SIGTERM/SIGINT 走優雅停機：
in-flight 任務退回 pending 自動重排、session 標中斷，不再留下永遠 running 的幽靈 meta 或無聲
從零重跑。

獨立於 ti.service 跑,所以重佈（restart ti.service）不會打斷自己；狀態存在 backlog 檔,
崩潰/重啟可續跑。
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import logging
import os
import re
import signal
import socket
import sys
import time
import uuid

# 顯式綁定真 CancelledError：部分主迴圈測試會把模組級 asyncio 換成 stub（只帶
# sleep/to_thread），except 子句經 stub 取屬性會 AttributeError；直接 import 名稱免疫。
from asyncio import CancelledError
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import (
    backlog,
    claude_accounts,
    config,
    deploy,
    digest,
    events,
    git_cred,
    history,
    insights,
    notify,
    provider_quota,
    publisher,
    runner,
    secure_write,
)
from .orchestrator import StudioSession, parse_tasks

# repo identity 正規化的單一真相（host-aware，同 path 非 GitHub host 視為不同）已抽至
# repo_ident 模組；保留 `_repo_key` 名稱，既有守護測試與呼叫點不變。
from .repo_ident import repo_key as _repo_key

log = logging.getLogger("ti.autopilot")

# 心跳檔寫入唯一 choke point：與 backlog/history 同範式走 secure_write.secure_write_root
# （原子 tmp+rename + owner 驗證）。module-level alias 兼顧可被測試 monkeypatch。
secure_write_root = secure_write.secure_write_root

_GH = ["gh"]
_MERGED_TITLE_CACHE_TTL = 3600.0
# 同一個 autopilot loop 內 token/repo 設定視為穩定；cache key 不含 token，避免每輪重打 API。
_MERGED_TITLE_CACHE: dict[tuple[str, int], tuple[float, list[str]]] = {}
_PREFILTER_IMPLEMENTED_LANE = "prefilter-implemented"
_PREFILTER_IMPLEMENTED_NOTE = "[prefilter-implemented]"
# 自我重載：autopilot 跑討論依賴整個 studio 套件（orchestrator／experts／flow／providers…），
# 故監看整包 studio/*.py 的 mtime——只盯少數檔會漏掉 orchestrator-only 的部署（如 #218），
# 讓 autopilot 一直跑舊 orchestration 邏輯（self-reload 在任務之間做、安全）。


async def _run(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        runner.kill_process_group(proc)
        return -1, f"(逾時 {timeout}s)"
    return proc.returncode if proc.returncode is not None else -1, out.decode("utf-8", "replace")


def _git_cred_argv() -> list[str]:
    """git 認證注入 argv：共用 `git_cred` SSOT。移除舊 gh CLI helper 依賴。

    預設（新機制＋git 2.31+）回 []，認證改走 `_git_cred_env` 的 GIT_CONFIG_* env；
    legacy 閥開啟或 git <2.31 時回 `-c http.extraHeader=...` fallback（token 在 argv 短窗可見）。
    origin 恆為 github.com AUTOPILOT_REPO，url 省略採 github per-host 注入。
    """
    return git_cred.git_cred_argv(config.GITHUB_TOKEN)


def _git_cred_env() -> dict[str, str] | None:
    """git 認證注入 env：`config.GITHUB_TOKEN` 走 GIT_CONFIG_* extraheader（token 不進 argv/ps），
    與 os.environ 合併後回傳。無 token／legacy／git <2.31 時回 None（維持 `_run` 繼承 os.environ 的原行為，
    認證由 `_git_cred_argv` argv fallback 承接）。"""
    extra = git_cred.make_env(config.GITHUB_TOKEN)
    return {**os.environ, **extra} if extra else None


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


async def _prepare_clone(work_dir: str | None = None) -> str:
    """確保 working clone 存在且重置到 origin/<branch> 的乾淨狀態。回傳路徑。

    work_dir 預設主 clone(AUTOPILOT_WORK_DIR);調查旁路線傳獨立目錄(-inv)——調查
    唯讀,但主 worker 每任務 reset --hard+clean 會抽換檔案,共用 clone 會讓旁路的
    Expert 讀取不一致。
    """
    work = str(work_dir or config.AUTOPILOT_WORK_DIR)
    url = f"https://github.com/{config.AUTOPILOT_REPO}.git"
    branch = config.AUTOPILOT_BRANCH
    if not (Path(work) / ".git").exists():
        Path(work).parent.mkdir(parents=True, exist_ok=True)
        rc, out = await _run(
            ["git", *_git_cred_argv(), "clone", url, work], timeout=300, env=_git_cred_env()
        )
        if rc != 0:
            raise RuntimeError(f"clone 失敗：{out[-400:]}")
    await _run(
        ["git", *_git_cred_argv(), "fetch", "origin", branch],
        cwd=work,
        timeout=120,
        env=_git_cred_env(),
    )
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


def _reformat_count(fmt_output: str, check_output: str) -> int:
    """解析被重排的檔數（僅供 log 顯示）：優先取 `ruff format` 寫回輸出的
    「N file(s) reformatted」，取不到再數 `--check` 輸出的「Would reformat」行數，
    都沒有回 0（只影響 log 數字，不影響閘門判定）。"""
    m = re.search(r"(\d+)\s+files?\s+reformatted", fmt_output)
    if m:
        return int(m.group(1))
    return len(re.findall(r"(?mi)^would reformat", check_output))


async def _autoformat_recheck(clone: str, check_output: str) -> runner.RunOutput:
    """`ruff format --check` 紅時的自動修復：同一工作區 `ruff format` 寫回後重跑 `--check`。

    背景（#249）：專家寫完碼、pytest 全綠，卻因純格式漂移（如 studio/appraisal.py 需
    reformat）被 lint 閘門整場退回，連續三輪各燒 1-2 小時只為空格。格式是機器可修的
    確定性問題，這裡直接修掉再重驗：重驗綠 → 視同通過（寫回的檔案由 run_one_task 後續
    _commit_push_merge 的 `git add -A` 兜底 commit 自然帶上）；重驗仍紅（ruff 版本漂移等
    罕見情況）→ 回傳重驗結果，由呼叫端維持原退回行為。`ruff check`（語意 lint）不在此列，
    照舊直接退回——自動修復僅限純排版，絕不動程式邏輯。
    """
    fmt = await runner.run_command_exec(
        clone,
        [sys.executable, "-m", "ruff", "format", "."],
        timeout=120,
        sandbox=True,
        label="ruff format",
    )
    recheck = await runner.run_command_exec(
        clone,
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        timeout=120,
        sandbox=True,
        label="ruff format --check",
    )
    if recheck.ok:
        log.info("格式已自動修正 %d 檔", _reformat_count(fmt.output, check_output))
    return recheck


async def _autolint_recheck(clone: str, check_output: str) -> runner.RunOutput:
    """`ruff check` 紅時的自動修復：`ruff check --fix` 寫回後重跑 `ruff check`。

    背景：import 排序（I001）、未用 import（F401）這類是機器可確定性修復的，卻因自動
    修復原本只掛在 `ruff format`、沒掛 `ruff check`，一路擋死到 3 次用罄（實例 #496/#364/
    #367）。`ruff check --fix`（**不帶** --unsafe-fixes）預設只套 ruff 標記為 safe 的修正
    ——語意保持、與 CI 跑的 `ruff check .`（同一 pin 版本）判定一致；重驗綠 → 視同通過
    （寫回檔由後續 _commit_push_merge 的 `git add -A` 兜底 commit 帶上）。E402「import not
    at top」等非 safe-fixable 規則 --fix 修不掉 → 重驗仍紅，回傳結果由呼叫端維持原退回
    行為（真正該退回的照樣退，autofix 不是無條件放行）。
    """
    fix = await runner.run_command_exec(
        clone,
        [sys.executable, "-m", "ruff", "check", "--fix", "."],
        timeout=120,
        sandbox=True,
        label="ruff check --fix",
    )
    recheck = await runner.run_command_exec(
        clone,
        [sys.executable, "-m", "ruff", "check", "."],
        timeout=120,
        sandbox=True,
        label="ruff check",
    )
    if recheck.ok:
        log.info("lint 已自動修正（ruff check --fix）：%s", (fix.output or "").strip()[:200])
    return recheck


async def _gate_lint(clone: str) -> tuple[bool, str]:
    """對齊 CI lint job：ruff check + ruff format --check。

    討論／pytest 閘門都不跑 ruff，lint 問題（未用 import、格式漂移等）會一路綠燈進
    main 卻在 GitHub CI 紅。此閘門補上。ruff 未安裝時 fail-open（只記警告不擋），避免
    部署環境缺 ruff 害死所有任務；裝了 ruff 才硬性把關。

    機器可修項自動修（config.LINT_AUTOFORMAT 開啟，預設開）：
    - `ruff check` 紅時先 `ruff check --fix`（僅套 ruff 標記為 safe 的修正，如 I001
      import 排序、F401 未用 import——皆語意保持）寫回再重驗，綠了視同通過（見
      _autolint_recheck）；E402「import not at top」等 **非** safe-fixable 規則 --fix
      修不掉，重驗照舊紅、照舊退回。
    - `ruff format --check` 紅時先 `ruff format` 寫回再重驗（見 _autoformat_recheck）。
    兩者重驗仍紅則維持原退回行為。背景：import 排序這類機器可確定性修復的問題，卻因
    自動修只掛在 format、沒掛 check，一路擋死到 3 次用罄（實例 #496/#364/#367）。
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
        if not r.ok and config.LINT_AUTOFORMAT:
            # 機器可修項先自動修再重驗，綠了就不退回整場討論（詳見各 _auto*_recheck）。
            if name == "ruff check":
                r = await _autolint_recheck(clone, r.output)
            elif name == "ruff format --check":
                r = await _autoformat_recheck(clone, r.output)
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


# --- 每日 PR 成本熔斷與 audit.jsonl 審計 -----------------------------------
#
# audit.jsonl 是結構化審計紀錄，也是每日 PR 計數的 SSOT：append-only、單一 writer
# （autopilot 主迴圈），免檔鎖；跨日重置由 ts 過濾天然實現；重啟不丟計數。
# 每日計數口徑＝「UTC 當日實際開出 PR（pr 非空）」——PR 開了但 CI 紅被關也燒了成本，
# 計入才符合「成本熔斷」語意。量級低（每日數十筆內）全檔掃描即可；無輪替（已知限制）。


def _audit_path() -> Path:
    return config.AUTOPILOT_STATE_DIR / "audit.jsonl"


# audit.jsonl 壓實門檻與保留天數：超過大小即把「保留期外」的舊紀錄搬到 audit.jsonl.old
# （冷歸檔，append-only），現役檔只留近期——每日計數只看「UTC 當日」，保留 30 天遠大於
# 計數窗口，壓實絕不影響熔斷口徑。收斂為純模組常數、不開 env override（對齊
# AUTOPILOT_DEDUP_RATIO 慣例）；5MB ≈ 數萬筆，正常量級多年才會觸發。
_AUDIT_MAX_BYTES = 5 * 1024 * 1024
_AUDIT_KEEP_DAYS = 30


def _maybe_compact_audit(path: Path) -> None:
    """audit.jsonl 超過大小門檻時壓實：保留期外舊紀錄搬 .old、現役檔原子重寫。

    壞行（解析不出 ts）視為舊紀錄一併歸檔，保證壓實後必縮小；全部都在保留期內
    （極端高量）則不動——寧可讓檔案暫時超標，也不丟仍在計數窗口附近的紀錄。
    單一 writer（autopilot 主迴圈）呼叫，無並寫競態。
    """
    if path.stat().st_size <= _AUDIT_MAX_BYTES:
        return
    cutoff = time.time() - _AUDIT_KEEP_DAYS * 86400
    keep: list[str] = []
    old: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ts = float(json.loads(line).get("ts", 0))
        except (ValueError, TypeError):
            ts = 0.0  # 壞行視為舊紀錄歸檔
        (keep if ts >= cutoff else old).append(line)
    if not old:
        return  # 全在保留期內：不重寫（見 docstring）
    archive = path.with_suffix(".jsonl.old")
    if not archive.exists():
        secure_write_root(archive, b"")
    with archive.open("a", encoding="utf-8") as f:
        f.write("\n".join(old) + "\n")
    # 現役檔走 secure_write_root 原子重寫（tmp+rename，維持 root-owner 不變量）
    body = ("\n".join(keep) + "\n") if keep else b""
    secure_write_root(path, body.encode("utf-8") if isinstance(body, str) else body)
    log.info(
        "audit.jsonl 壓實：歸檔 %d 筆、保留 %d 筆（近 %d 天）",
        len(old),
        len(keep),
        _AUDIT_KEEP_DAYS,
    )


def _append_audit(rec: dict) -> None:
    """append 一筆審計紀錄到 audit.jsonl。

    首次以 secure_write_root 建空檔（root owner，鏡射 history.record_event 範式，
    維持 REQUIRE_CHOWN 不變量），之後 open("a") append；超過大小門檻順手壓實
    （見 _maybe_compact_audit）。審計只是可觀測性，任何寫入失敗都不得弄死主迴圈
    （僅留 debug log）。
    """
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            secure_write_root(path, b"")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_compact_audit(path)
    except Exception:  # noqa: BLE001 — 審計失敗不影響主迴圈
        log.debug("audit.jsonl 寫入失敗（忽略，不影響主迴圈）", exc_info=True)


def _todays_pr_count(now: float | None = None) -> int:
    """統計 audit.jsonl 中 UTC 當日且實際開出 PR（pr 非空）的筆數；壞行/壞 ts 跳過。"""
    path = _audit_path()
    if not path.is_file():
        return 0
    day = time.gmtime(now if now is not None else time.time())[:3]
    n = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("pr") is None:
                continue
            if time.gmtime(float(rec.get("ts", 0)))[:3] == day:
                n += 1
        except (ValueError, TypeError):
            continue
    return n


def _daily_pr_budget_exceeded(now: float | None = None) -> bool:
    """每日 PR 預算是否已用滿。budget <= 0＝不限制（預設，行為不變）。"""
    budget = config.AUTOPILOT_DAILY_PR_BUDGET
    if budget <= 0:
        return False
    return _todays_pr_count(now) >= budget


def _next_utc_midnight(now: float) -> float:
    """下一個 UTC 零點的 epoch 秒（每日預算的自動恢復時刻）。"""
    day = time.gmtime(now)
    return float(calendar.timegm((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0)) + 86400)


class MergeResult(tuple):
    """`(ok, msg)` 二元組的向後相容擴充：額外攜帶 pr_number / branch 供落檔追溯。

    刻意繼承 tuple 並固定只裝兩個元素——既有呼叫端（含大量守護測試）的
    `ok, msg = await _commit_push_merge(...)` 解包完全不受影響；需要 PR 編號／
    合併分支的呼叫端（run_one_task 落檔 pr/merged_branch）用屬性取。
    （不設 __slots__：tuple 子類不支援非空 __slots__，屬性走一般 __dict__。）
    """

    def __new__(
        cls,
        ok: bool,
        msg: str,
        *,
        pr_number: int | None = None,
        branch: str = "",
        no_changes: bool = False,
        auto_merge_pending: bool = False,
    ):
        self = super().__new__(cls, (ok, msg))
        return self

    def __init__(
        self,
        ok: bool,
        msg: str,
        *,
        pr_number: int | None = None,
        branch: str = "",
        no_changes: bool = False,
        auto_merge_pending: bool = False,
    ):
        self.pr_number = pr_number
        self.branch = branch
        # auto_merge_pending=True：PR 已開、GitHub 原生 auto-merge 已掛上，但快窗內 CI 尚未
        # 收斂——PR 留在遠端由 GitHub 背景合併，呼叫端把任務標 merging 交 reconciler 收尾，
        # 不關 PR、不算失敗（成品不再因 CI 慢於等待窗而被丟棄）。
        self.auto_merge_pending = auto_merge_pending
        # no_changes=True：專家跑完但零 diff（無 commit 可合併）。這不是「合併失敗」而是
        # 「沒有可出貨的變更」——呼叫端據此把任務收斂為 parked no-op（不燒重試、不進失敗桶），
        # 而非走 _handle_gate_failure 白燒 3 次 session（見完成率診斷：PR 階段 16/22 為此類）。
        self.no_changes = no_changes


# 網路層「暫時性」失敗特徵（DNS/連線/5xx/逾時）：merge 階段（ls-remote/push/開 PR）遇這類
# ——非任務缺陷、非認證/權限實質失敗——附 infra 標記，讓 backlog.triage_failed 在達重試上限後
# 自動重排（對齊既有「逾時→triage 重試」行為）。刻意「不」涵蓋認證/權限字樣（permission
# denied / 403 / authentication failed / could not read Username），那類是實質失敗、達上限即
# 永久 failed（附 infra 標記會讓 triage 無限重排）。
_NETWORK_TRANSIENT_RE = re.compile(
    r"could not resolve host|unable to access|"
    r"connection (?:timed out|reset|refused)|operation timed out|"
    r"temporar(?:ily|y) (?:unavailable|failure)|\b50[234]\b|timed out|逾時",
    re.IGNORECASE,
)


def _merge_fail_note(msg: str, out: str) -> str:
    """merge 階段失敗訊息：偵測到網路暫時性特徵時附「unreachable」標記（INFRA_FAILURE_RE 命中
    → triage 自動重排）；認證/權限等實質失敗不附，達上限即永久 failed。"""
    if _NETWORK_TRANSIENT_RE.search(out or ""):
        return f"{msg}｜unreachable（網路暫時性，可分診重試）"
    return msg


async def _reclaim_stale_branch(clone: str, repo: str, branch: str) -> tuple[bool, str]:
    """認領遠端殘留的同名任務分支，讓被中斷的任務重跑時能照常出貨。

    殘留成因：前次執行在「等 CI→合併」期間被中斷（SIGTERM/execv 重載/crash），PR 與分支
    留在遠端；重跑走到 push 前防呆撞見同名分支，舊行為一律中止＝任務被自己的殘留永久擋死
    （分支名由 task id 決定，殘留必屬本任務前次執行，認領不會動到別的任務）。
    處置：有 open PR → `gh pr close --delete-branch` 一併收掉；無 open PR（從未開出/已關閉/
    已合併但分支殘留）→ 直接刪遠端分支。刪除失敗（網路/權限）回 (False, 原因) 由呼叫端維持
    既有中止語意（fail-safe 不變），原因經 _merge_fail_note 標記讓暫時性失敗可分診重試。
    """
    rc, state = await _run(
        [*_GH, "pr", "view", branch, "-R", repo, "--json", "state", "-q", ".state"],
        cwd=clone,
        timeout=60,
    )
    if rc == 0 and state.strip().upper() == "OPEN":
        rc, out = await _run(
            [*_GH, "pr", "close", "-R", repo, branch, "--delete-branch"],
            cwd=clone,
            timeout=120,
        )
        if rc != 0:
            return False, _merge_fail_note(
                f"認領殘留分支 {branch} 失敗（關閉殘留 PR 未成，已中止）：{out[-400:]}", out
            )
        return True, "已關閉殘留 open PR 並刪除分支"
    rc, out = await _run(
        ["git", *_git_cred_argv(), "push", "origin", "--delete", branch],
        cwd=clone,
        timeout=120,
        env=_git_cred_env(),
    )
    if rc != 0:
        return False, _merge_fail_note(
            f"認領殘留分支 {branch} 失敗（刪除遠端分支未成，已中止）：{out[-400:]}", out
        )
    return True, "已刪除殘留遠端分支"


async def _fast_wait_auto_merge(pr_number: int) -> bool:
    """auto-merge 掛上後的短窗輪詢（完成率第三輪修法二B）。

    多數 CI 幾分鐘內就綠：窗內（AUTOPILOT_MERGE_FAST_WAIT 秒）合併完成即回 True，行為與
    既有同步路徑等價但等待短得多；窗滿仍未合併回 False——PR 留在遠端由 GitHub 背景合併，
    呼叫端把任務標 merging 交 reconciler 收尾。快窗只回答「已合併了嗎」：真衝突/CI 紅/
    被關閉等一律留給 reconciler 統一處置（那裡有完整分支）。FAST_WAIT=0＝掛上即走。
    """
    deadline = time.time() + max(config.AUTOPILOT_MERGE_FAST_WAIT, 0)
    while time.time() < deadline:
        data = await publisher._get_pr_status(pr_number, retries=0)
        if data and data.get("merged"):
            return True
        await asyncio.sleep(config.PUBLISH_CI_INTERVAL)
    return False


async def _commit_push_merge(clone: str, task: dict) -> tuple[bool, str]:
    """把成果開分支、push、squash-merge 進 main。dryrun 只回報。

    成功（已合併）與「PR 已開但 CI 未過/合併失敗」時回傳 MergeResult——解包仍是
    (ok, msg)，另帶 pr_number / branch 屬性供 run_one_task 落檔與 audit 計數
    （呼叫端以 getattr 容錯讀取，未開到 PR 的失敗路徑維持純 tuple）。
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

    # 每日 PR 成本熔斷（兜底 guard，獨立於上方 repo 污染防護、順序在後）：正常路徑已在
    # 主迴圈與 run_one_task 先擋，此處只防未來新呼叫端繞過。dryrun 不打真 PR，不受限。
    if not config.AUTOPILOT_DRYRUN and _daily_pr_budget_exceeded():
        return False, (
            f"已達每日 PR 預算 {config.AUTOPILOT_DAILY_PR_BUDGET}，已中止推送（UTC 跨日自動恢復）"
        )

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
            # 零 diff：不是合併失敗，而是「沒有可出貨的變更」。以 no_changes 旗標讓呼叫端
            # 收斂為 parked no-op（不燒重試、不落入失敗桶），而非白燒 3 次 session。
            return MergeResult(False, "沒有產生任何變更（無 commit 可合併）", no_changes=True)

        if config.AUTOPILOT_DRYRUN:
            return True, f"[dryrun] 會 push {branch} 並 squash-merge 進 {config.AUTOPILOT_BRANCH}"

        # push 前防呆：每個 task 都是全新分支，遠端不該已存在同名分支。三態判定——
        #   rc!=0：ls-remote 本身失敗（網路/認證），視為錯誤中止，不可 fall-through 當「不存在」。
        #   rc==0 且有輸出：遠端已存在同名分支（task 重跑或殘留），預設中止；FORCE_PUSH 為真才放行覆寫。
        #   rc==0 且空輸出：遠端不存在，放行。
        rc, out = await _run(
            ["git", *_git_cred_argv(), "ls-remote", "--heads", "origin", branch],
            cwd=clone,
            timeout=60,
            env=_git_cred_env(),
        )
        if rc != 0:
            return False, _merge_fail_note(
                f"ls-remote 檢查失敗（無法確認遠端狀態，已中止）：{out[-400:]}", out
            )
        if out.strip() and not config.AUTOPILOT_FORCE_PUSH:
            if not config.AUTOPILOT_RECLAIM_BRANCH:
                return False, (
                    f"遠端已存在同名分支 {branch}，為避免覆寫已中止；"
                    f"如確認要覆寫殘留分支，設 TI_AUTOPILOT_FORCE_PUSH=1"
                )
            # 認領殘留分支（B-4）：殘留必屬本任務前次被中斷的執行，關舊 PR/刪舊分支後
            # 照常走「push→開新 PR」，任務不再被自己的殘留永久擋死。失敗維持中止語意。
            reclaimed, note = await _reclaim_stale_branch(clone, repo, branch)
            if not reclaimed:
                return False, note
            log.info("任務 #%s 認領殘留分支 %s：%s", task["id"], branch, note)

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
            ["git", *_git_cred_argv(), "push", *push_flags, "-u", "origin", branch],
            cwd=clone,
            timeout=180,
            env=_git_cred_env(),
        )
        if rc != 0:
            return False, _merge_fail_note(f"push 失敗：{out[-400:]}", out)
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
            return False, _merge_fail_note(f"開 PR 失敗：{out[-400:]}", out)

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

        # 原生 auto-merge（完成率第三輪修法二B，預設開）：把「等 CI→合併」交還 GitHub——
        # 舊同步路徑阻塞等 CI 最長 600s，期間被 SIGTERM/execv 打斷＝殘留 open PR；CI 慢於
        # 600s ＝關 PR 丟掉整份成品重跑。掛上 auto-merge 後短窗輪詢：窗內合併＝原成功路徑
        # 零改動；窗滿仍 pending＝不關 PR、回 auto_merge_pending，任務標 merging 續跑下一場，
        # 由 reconciler 收尾（strict:true 的 BEHIND 也在那裡 update-branch 解鎖）。
        # 掛失敗（PR 已 clean 可直合、API 錯、repo 未開 allow_auto_merge）→ 回退同步路徑，行為不變。
        if config.AUTOPILOT_AUTO_MERGE:
            rc, out = await _run(
                [*_GH, "pr", "merge", str(pr_number), "-R", repo, "--auto", "--squash"],
                cwd=clone,
                timeout=60,
            )
            if rc == 0:
                if await _fast_wait_auto_merge(pr_number):
                    return MergeResult(
                        True,
                        f"已 squash-merge {branch} 進 {config.AUTOPILOT_BRANCH}"
                        f"（auto-merge，CI 綠後由 GitHub 合併）",
                        pr_number=pr_number,
                        branch=branch,
                    )
                return MergeResult(
                    False,
                    f"auto-merge 已掛上 PR #{pr_number}，CI 未於快窗內收斂——"
                    f"留待 GitHub 背景合併（reconciler 收尾）",
                    pr_number=pr_number,
                    branch=branch,
                    auto_merge_pending=True,
                )
            log.warning("掛 auto-merge 失敗（回退同步等 CI 路徑）：%s", (out or "").strip()[-200:])

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
        # 非綠/未合併：關閉 PR 並刪分支，避免留下孤兒 PR。
        # 注意：機械性 BEHIND（落後 base）已在 _merge_flow 內自動 update-branch→等 CI→
        # 重試合併（TI_MERGE_BEHIND_RETRIES 輪），不會走到這裡；至此仍失敗＝額度用盡／
        # 真衝突／CI 紅等實質問題。維持關閉＋刪分支：任務退回重跑會開同名分支，
        # 殘留舊分支反而會撞上前面的 ls-remote 防覆寫中止。
        await _run(
            [*_GH, "pr", "close", "-R", repo, branch, "--delete-branch"],
            cwd=clone,
            timeout=120,
        )
        # 用 MergeResult 攜帶 pr_number：PR 已實際開出（燒了 CI/API 成本），audit 與每日
        # PR 預算需計入；解包仍是 (False, msg)，既有呼叫端不受影響。
        fail_msg = f"CI 未過或合併失敗（{outcome.value if hasattr(outcome, 'value') else outcome}）：{detail}"
        # 分診閉環（B-5）：等 CI 逾時／API·網路錯誤是暫時性 infra 問題（非本任務程式碼缺陷），
        # 附 unreachable 標記讓 backlog.triage_failed（INFRA_FAILURE_RE）在達重試上限後自動重排；
        # CI 紅（ci_failed）／真衝突／被保護擋下是實質失敗，刻意不附（附了會無限重排）。
        if outcome in (publisher.MergeOutcome.TIMEOUT, publisher.MergeOutcome.ERROR):
            fail_msg += "｜unreachable（網路暫時性，可分診重試）"
        return MergeResult(False, fail_msg, pr_number=pr_number, branch=branch)
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


def _first_similar_title(title: str, corpus: Iterable[str]) -> str | None:
    """相似度去重的共用判定：回傳 corpus 中第一個與 title 詞集 Jaccard 相似度
    ≥ `AUTOPILOT_DEDUP_RATIO` 的標題（供 debug log 指出「近似哪一筆」），皆不相似回 None。

    done（`improver._discover`）與 pending（`_filter_pending_duplicates`）兩條防線共用此單一來源，
    杜絕第二套實作漂移。corpus 收 `Iterable[str]`（不限死 list）並維持其迭代順序、第一個命中即短路；
    helper 內不對 corpus 排序（排序是無收益擾動）。corpus 為空時自然回 None——`AUTOPILOT_EVAL_MEMORY=0`
    使 done corpus 為空，與舊精確比對關閉行為逐位等價，向後相容不加分支。
    """
    return next(
        (e for e in corpus if _token_set_similarity(title, e) >= config.AUTOPILOT_DEDUP_RATIO),
        None,
    )


_MERGE_SUBJECT_RE = re.compile(
    r"^(merge pull request #\d+\b|merge branch\b|merge remote-tracking branch\b)", re.IGNORECASE
)


def _enough_title_signal(title: str) -> bool:
    """低資訊標題不參與「疑似已實作」判定，避免 `fix tests` 這類短句誤殺。"""
    return len(_tokenize_for_dedup(title)) >= 3


def _first_similar_implemented_title(
    title: str,
    merged_titles: Iterable[str],
    *,
    threshold: float | None = None,
) -> str | None:
    """回傳第一個與任務 title 相似的已 merged 標題；純比對，不讀 backlog/網路/git。

    相似度複用 `_token_set_similarity`，並對任務標題與語料標題都套低資訊保護（token < 3
    直接跳過）。threshold 預設讀 `AUTOPILOT_PREFILTER_RATIO`，測試可注入固定值。
    """
    threshold = config.AUTOPILOT_PREFILTER_RATIO if threshold is None else threshold
    if threshold <= 0 or not _enough_title_signal(title):
        return None
    for merged in merged_titles:
        if _enough_title_signal(merged) and _token_set_similarity(title, merged) >= threshold:
            return merged
    return None


def _with_prefilter_note(task: dict, note: str, *, limit: int = 500) -> str:
    """prefilter 分流後保留匹配 merged title，避免後續調查出口覆寫稽核線索。"""
    existing = str(task.get("note") or "").strip()
    if (task.get("lane") or "") == _PREFILTER_IMPLEMENTED_LANE and existing.startswith(
        _PREFILTER_IMPLEMENTED_NOTE
    ):
        # 重試或多出口共用時避免把同一段 prefilter note 重複拼回去。
        note = existing if note.startswith(existing) else f"{existing}\n{note}"
    return note[:limit]


def _sanitize_prefilter_title(title: str, *, limit: int = 200) -> str:
    """把外部 merged title 壓成單行短字串，降低 prompt marker 偽造風險。"""
    cleaned = re.sub(r"\s+", " ", str(title)).strip()
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip()
    return cleaned


def _commit_message_title(message: str) -> str:
    """從 git log 的 commit message 取可比對標題；GitHub merge subject 會跳過取 body PR title。"""
    for line in message.splitlines():
        title = line.strip()
        if not title or _MERGE_SUBJECT_RE.match(title):
            continue
        return title
    return ""


def _extract_git_log_titles(log_output: str) -> list[str]:
    """解析 `git log --format=%B%x00` 輸出為標題清單，維持 git log 由新到舊順序。"""
    return _dedupe_titles(_commit_message_title(chunk) for chunk in log_output.split("\x00"))


def _dedupe_titles(titles: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for title in titles:
        clean = (title or "").replace("\r", " ").replace("\n", " ").strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _parse_github_datetime(value: str) -> datetime | None:
    with contextlib.suppress(ValueError):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


async def _fetch_github_merged_titles(repo: str, since_days: int) -> list[str] | None:
    """以 GitHub REST 取近期 merged PR title；None 代表 API 不可用，呼叫端應 fallback。"""
    token = (config.GITHUB_TOKEN or "").strip()
    repo = (repo or "").strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None

    try:
        import httpx

        cutoff = datetime.now(UTC) - timedelta(days=max(0, since_days))
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        params = {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100}
        # trust_env 刻意維持預設：外網 client 允許企業 proxy / 自訂 CA，關閉會破壞企業環境路由
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/pulls",
                headers=headers,
                params=params,
            )
        if resp.status_code != 200:
            log.debug("merged PR 標題 API 不可用（status=%s），改用 git log", resp.status_code)
            return None
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("merged PR 標題 API 失敗，改用 git log：%s", exc)
        return None

    titles: list[str] = []
    for pr in rows if isinstance(rows, list) else []:
        if not isinstance(pr, dict):
            continue
        merged_at = pr.get("merged_at")
        title = str(pr.get("title") or "").strip()
        if not merged_at or not title:
            continue
        merged_dt = _parse_github_datetime(str(merged_at))
        if merged_dt is not None and merged_dt >= cutoff:
            titles.append(title)
    return _dedupe_titles(titles)


async def _fetch_git_log_merged_titles(clone: str, since_days: int) -> list[str]:
    """離線 fallback：從本地 git log 取近期 commit/merge message 標題。

    known-limitation：shallow clone、無歷史或非 git 目錄會回空清單，呼叫端因此放行（漏判優於誤殺）。
    不使用 `--merges --oneline`，因 GitHub merge commit 的 subject 多半只有 PR 編號，真正 PR
    title 在 body；`%B%x00` 才能穩定取到。
    """
    if since_days <= 0:
        return []
    rc, out = await _run(
        ["git", "log", "--format=%B%x00", f"--since={since_days}.days.ago"],
        cwd=clone,
        timeout=30,
    )
    if rc != 0:
        log.debug("git log merged 標題 fallback 失敗：%s", out[-200:])
        return []
    return _extract_git_log_titles(out)


async def _fetch_merged_titles(clone: str, repo: str, since_days: int) -> list[str]:
    """取近期已合併標題語料：GitHub merged PR title 優先，離線 fallback `git log`。

    僅取 GitHub closed PR 第一頁（per_page=100）；活躍 repo 可能漏掉更舊 PR，但偏誤方向是
    放行而非誤降級。結果快取 1 小時，避免同一輪多任務重複打 API。
    """
    repo = (repo or "").strip()
    since_days = max(0, int(since_days))
    key = (repo, since_days)
    now = time.time()
    cached = _MERGED_TITLE_CACHE.get(key)
    if cached and now - cached[0] < _MERGED_TITLE_CACHE_TTL:
        return list(cached[1])

    titles = await _fetch_github_merged_titles(repo, since_days)
    if titles is None:
        titles = await _fetch_git_log_merged_titles(clone, since_days)
    titles = _dedupe_titles(titles)
    _MERGED_TITLE_CACHE[key] = (now, titles)
    return list(titles)


async def _recent_merged_title_corpus(clone: str, repo: str | None = None) -> list[str]:
    """套 config 旋鈕取近期 merged title 語料；總開關關閉時直接回空。"""
    if not config.AUTOPILOT_PREFILTER_IMPLEMENTED:
        return []
    return await _fetch_merged_titles(
        clone,
        repo or config.AUTOPILOT_REPO,
        config.AUTOPILOT_PREFILTER_LOOKBACK_DAYS,
    )


async def _prefilter_implemented_match(task: dict, clone: str) -> str | None:
    """pick 後、跑完整管線前檢查任務是否疑似已由近期 merged PR 實作。"""
    if not config.AUTOPILOT_PREFILTER_IMPLEMENTED:
        return None
    if (task.get("lane") or "") == "full":
        return None
    title = str(task.get("title") or "")
    if not _enough_title_signal(title):
        return None
    try:
        corpus = await _recent_merged_title_corpus(clone)
    except Exception as exc:  # noqa: BLE001
        log.debug("疑似已實作 prefilter 失敗，放行完整管線：%s", exc)
        return None
    return _first_similar_implemented_title(title, corpus)


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
        hit = _first_similar_title(p, existing_titles)
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


# discovered followup 的「證據儀式」訊號：無會改動程式碼的客觀完成判準的自我指涉 meta 任務特徵
# （收尾驗收/權威證據檔/closure 報告/sha256 落檔/重跑並回報…）。命中任一僅代表「疑似 busywork」，
# 需再經 _is_low_value_followup 確認「同時缺 code-work 訊號」才丟棄。與去重防線（F）互補：去重擋
# 「重複」，本閘擋「全新但同樣沒價值」。
_FOLLOWUP_BUSYWORK_RE = re.compile(
    r"收尾驗收|驗收\s*pass|QA\s*pass|權威(證據|報告|判定|聲明|宣告)?"
    r"|sha256|落(檔|盤)|closure|閉環|重驗|複核|蒐證|凍結|handoff|evidence"
    r"|報告檔|慣例(說明|定義)|\$TMPDIR|(重跑|再跑|重新執行).{0,20}(回報|確認|驗收|附上)",
    re.IGNORECASE,
)

# code-work 豁免訊號：任一實作/修復/測試/守門動詞即視為「有會改碼的交付」→ 一律保留（保守：寧放勿殺，
# 只要沾一點真實工作就不誤殺）。只讓「純落檔/純重跑回報/純寫慣例文件/純產 evidence」落網。
_FOLLOWUP_CODEWORK_RE = re.compile(
    r"實作|實做|實裝|修復|修正|修掉|重構|改造"
    r"|新增.{0,6}(功能|測試|守門|欄位|API|按鈕|旗標|參數|防護)"
    r"|補.{0,4}測試|加.{0,6}(測試|守門|檢查|防護|timeout)|守門測試|斷言"
    r"|implement|refactor|\bfix(es|ed)?\b",
    re.IGNORECASE,
)


def _is_low_value_followup(title: str, detail: str = "") -> bool:
    """良構性/價值閘：命中「證據儀式」busywork 訊號 AND 缺任何 code-work 訊號 → 判低價值（丟棄）。

    雙條件刻意保守：只要標題/detail 帶任一實作/修復/測試/守門動詞即豁免（偏向寧放勿殺），只狙擊
    「純落檔/純重跑回報/純寫慣例文件/純產 evidence」這類**無會改動程式碼的客觀完成判準**的自我指涉
    meta 任務——它們正是「討論永不收斂／生成檔 lint 修不掉／零-diff merge」三個失敗桶的共同上游
    （見完成率第二輪診斷）。`AUTOPILOT_FOLLOWUP_VALUE_GATE=0` 可即時停用、恢復舊行為。
    """
    if not config.AUTOPILOT_FOLLOWUP_VALUE_GATE:
        return False
    text = f"{title}\n{detail}"
    if not _FOLLOWUP_BUSYWORK_RE.search(text):
        return False
    if _FOLLOWUP_CODEWORK_RE.search(text):
        return False
    return True


# 調查訊號：任務標題/細節帶這些動詞/名詞＝「產出結論」型工作（調查/驗證/盤點/量測/診斷…），
# 其正確完成判準是結論本身，不是 code diff。與 _FOLLOWUP_BUSYWORK_RE 聯集使用——證據儀式類
# （sha256/權威/落檔）雖被價值閘擋在 followup 進場，但既有存量與其他來源仍會入場，一併分流。
_INVESTIGATION_RE = re.compile(
    r"調查|查明|查清|釐清|盤點|稽核|驗證|確認|複核|檢核|核對|量測|評估|診斷|分診|歸因|比對|證據"
    r"|investigate|audit|verify|diagnose|measure",
    re.IGNORECASE,
)


def _is_investigation_task(task: dict) -> bool:
    """確定性分類：這個任務該走「調查分流輕量管線」嗎（完成率第三輪修法一）？

    雙條件保守設計（與 _is_low_value_followup 同哲學、共用既有 regex）：
    命中調查訊號（_INVESTIGATION_RE 或 _FOLLOWUP_BUSYWORK_RE）**且**無任何 code-work
    豁免訊號（_FOLLOWUP_CODEWORK_RE）才分流；拿不準（有沾實作/修復/測試動詞）一律走
    原多專家管線——分流錯過只是回到現狀，分流誤入有 `需改碼:` 安全閥退回，兩向皆有兜底。
    task 帶 lane="full"（前次調查判定需改碼、已升級）者不再分流，防止乒乓迴圈。
    """
    if not config.AUTOPILOT_INVESTIGATION_LANE:
        return False
    if (task.get("lane") or "") == "full":
        return False
    text = f"{task.get('title') or ''}\n{task.get('detail') or ''}"
    if not (_INVESTIGATION_RE.search(text) or _FOLLOWUP_BUSYWORK_RE.search(text)):
        return False
    if _FOLLOWUP_CODEWORK_RE.search(text):
        return False
    return True


def _build_investigation_prompt(task: dict) -> str:
    """組裝「單專家調查」prompt（純字串、無 LLM/網路，可單測；範式同 _build_split_prompt）。

    關鍵設計（對治驗屍死因）：交付物＝結論文字本身，明令**禁止落檔**（$TMPDIR 落檔正是
    「QA 換 shell 讀不到 → 每輪 FAIL 同因」的結構性死因）；強制 `證據:` 行防單專家自說自話；
    `需人工:`/`需改碼:` 兩個結構化出口讓 AI 做不到/誤分類的任務走對的路，而非硬耗到 failed。
    """
    title = (task.get("title") or "").strip()
    detail = (task.get("detail") or "").strip()
    base = f"任務標題：{title}\n"
    if detail:
        base += f"任務細節：{detail}\n"
    note = (task.get("note") or "").strip()
    if note:
        base += f"任務備註：{note}\n"
    return (
        "你是資深工程師，獨立完成以下這項「調查/驗證」型任務。交付物是**你的文字結論本身**"
        "（會直接寫回任務看板與教訓庫），不是程式碼、不是檔案。\n\n"
        f"{base}\n"
        "要求：\n"
        "1. 就地以唯讀方式調查（讀碼/grep/跑唯讀指令皆可），**不要修改任何檔案、不要把結論"
        "落檔到 $TMPDIR 或任何路徑**——你的文字輸出就是唯一交付物。\n"
        "2. 輸出格式（缺 `結論:` 即視為調查未完成）：\n"
        "結論: <一段可獨立理解的結論：直接回答任務要查的問題，講清楚答案與根因>\n"
        "證據: <支撐結論的關鍵證據，如 檔案:行號、指令與輸出摘要；可多行，每行以「證據:」開頭>\n"
        "後續任務: <調查若發現需要改碼的具體工作，每行一項、動詞開頭；沒有就不列>\n"
        "3. 若此任務 AI 做不到、必須人工處理（如換發 token、外部服務後台操作），改輸出一行：\n"
        "需人工: <一句原因>\n"
        "4. 若你判斷此任務其實必須實際改動程式碼才算完成（不是純調查），**不要動手改**，改輸出一行：\n"
        "需改碼: <一句原因>\n"
    )


_REFUTER_SYSTEM = """你是嚴格的審稿人（refuter），唯一職責是試圖推翻一份調查結論。

只審兩件事：
1. 證據是否真的支撐結論——證據與結論無關、檔案:行號明顯對不上主題、或證據只是重述結論本身，都算推得翻。
2. 結論是否回答了任務要查的問題——答非所問、只描述過程沒有給出答案，也算推得翻。

你不需要重做調查，也不要求證據窮盡；只在結論有明顯破綻時才判成立。**拿不準一律判不成立**
（寧放勿殺——誤殺合法結論的代價是整場重查）。

輸出最後一行固定格式（擇一）：
反駁: 成立 <一句具體破綻>
反駁: 不成立"""


async def _refute_investigation(task: dict, parsed: dict, clone: str, sid: str) -> str:
    """調查結論的對抗性驗證：一次廉價 MODEL_FAST 呼叫（providers.complete_once，永不
    raise），試圖推翻結論。回傳破綻字串（非空＝反駁成立）；旋鈕關閉、refuter 判不成立、
    離線/壞輸出/逾時一律回空字串（寧放勿殺，見 AUTOPILOT_INVESTIGATION_REFUTE）。"""
    from . import flow, providers

    if not config.AUTOPILOT_INVESTIGATION_REFUTE:
        return ""
    evidence = "\n".join(f"證據: {e}" for e in parsed["evidence"])
    user = (
        f"任務標題：{(task.get('title') or '').strip()}\n"
        + (f"任務細節：{(task.get('detail') or '').strip()}\n" if task.get("detail") else "")
        + f"\n待審結論：\n{parsed['conclusion']}\n\n附帶證據：\n{evidence}\n"
    )
    text = await providers.complete_once(
        _REFUTER_SYSTEM, user, session_id=f"{sid}:refute", cwd=Path(clone)
    )
    return flow.parse_refutation(text or "")


async def _run_investigation_task(
    task: dict, clone: str, sid: str, t0: float, *, sideline: bool = False
) -> None:
    """調查/驗證型任務的輕量管線：單專家一次 speak → 結構化結論寫回 backlog＋教訓庫。

    sideline=True＝由旁路併行線呼叫：心跳走 liveness_only,不得把 status.json 的
    state/task_id 蓋成旁路任務(主迴圈身分欄位;旁路顯示走 sideline 子欄)。
    不建 StudioSession、不過 lint/collect/test/merge 閘門——這類任務的完成判準是結論，
    不是 code diff（驗屍：多專家管線對它們結構上不可能過，見 AUTOPILOT_INVESTIGATION_LANE）。
    四個出口：
      1. `結論:` ＋至少一行 `證據:` → done（note 帶結論摘要）；結論進 lessons、
         `後續任務:` 走 _add_discovered_followups（與討論回填同一套防線＋扇出上限）。
      2. `需人工:` → parked（AI 做不到，不再重燒 session）。
      3. `需改碼:` → 退回 pending＋lane="full"（下輪走完整多專家管線），不消耗 attempts
         ——誤分類安全閥，分類錯誤的成本只是多一輪等待。
      4. 空輸出/缺結論/缺證據/逾時/例外 → 沿用 _handle_discussion_incomplete 既有重試語意。
    audit 記 investigation_done|investigation_parked|investigation_escalated（無 PR、pr=None
    不計每日預算）。任何未預期例外走出口 4，絕不讓分流弄死主迴圈。
    """
    from . import flow, lessons
    from .experts import Expert
    from .roles import SENIOR

    log.info("任務 #%s 走調查分流輕量管線（單專家、不開 PR）", task["id"])
    history.start_session(sid, f"[autopilot/調查] {task['title']}")
    heartbeat = asyncio.create_task(_task_heartbeat(task["id"], sid, liveness_only=sideline))
    text = ""
    try:
        ex = Expert(SENIOR, sid, Path(clone))

        async def _noop(_ev):
            return None

        try:
            text = await asyncio.wait_for(
                ex.speak(_build_investigation_prompt(task), _noop),
                timeout=config.AUTOPILOT_INVESTIGATION_TIMEOUT or None,
            )
        finally:
            with contextlib.suppress(Exception):
                await ex.stop()
    except Exception:  # noqa: BLE001 — 逾時/專家例外一律走「未收斂」重試出口，不冒泡
        log.warning(
            "任務 #%s 調查專家呼叫失敗/逾時，走討論未收斂重試語意", task["id"], exc_info=True
        )
        text = ""
    finally:
        heartbeat.cancel()
        with contextlib.suppress(BaseException):
            await heartbeat
        with contextlib.suppress(Exception):
            history.finish_session(sid)

    parsed = flow.parse_investigation(text or "")

    def _audit(outcome: str, detail: str) -> None:
        if config.AUTOPILOT_DRYRUN:
            return
        _append_audit(
            {
                "ts": time.time(),
                "task_id": task.get("id"),
                "pr": None,  # 調查管線不開 PR，不計每日 PR 預算
                "branch": "",
                "head_sha": "",
                "outcome": outcome,
                "detail": detail[-400:],
                "duration_s": round(time.time() - t0, 1),
                "attempts": int(task.get("attempts") or 0),
            }
        )

    if parsed["needs_human"]:
        note = f"[調查] 需人工：{parsed['needs_human']}"
        backlog.set_status(task["id"], "parked", note=_with_prefilter_note(task, note))
        _audit("investigation_parked", note)
        log.info("任務 #%s 調查判定需人工，parked：%s", task["id"], parsed["needs_human"])
        return
    if parsed["needs_code"]:
        # 升級回完整管線：不消耗 attempts（回填揀起前的值），lane="full" 防止再被分流。
        note = f"[調查] 判定需改碼，升級回完整多專家管線：{parsed['needs_code']}"
        backlog.set_status(
            task["id"],
            "pending",
            lane="full",
            attempts=int(task.get("attempts") or 0),
            note=_with_prefilter_note(task, note),
        )
        _audit("investigation_escalated", note)
        log.info("任務 #%s 調查判定需改碼，退回完整管線重跑（不耗 attempts）", task["id"])
        return
    if parsed["conclusion"] and parsed["evidence"]:
        summary = " ".join(parsed["conclusion"].split())
        # 對抗性驗證（refuter）：標 done 前派一次廉價呼叫專職試圖推翻——單專家調查的
        # 已知風險是結論頭頭是道、證據對不上（reward hacking），而結論會進教訓庫污染
        # 長期記憶。推得翻 → 不標 done，走既有重試語意（note 帶破綻，重查有據）；
        # 推不翻/refuter 壞掉 → 照常 done（寧放勿殺）。外層 suppress：refuter 是加值
        # 防線不是依賴，任何例外都不得擋住合法結論。
        refuted = ""
        with contextlib.suppress(Exception):
            refuted = await _refute_investigation(task, parsed, clone, sid)
        if refuted:
            _audit("investigation_refuted", refuted)
            log.info("任務 #%s 調查結論被反駁，退回重查：%s", task["id"], refuted[:160])
            _handle_discussion_incomplete(task, reason=f"調查結論被反駁：{refuted[:160]}")
            return
        backlog.set_status(
            task["id"], "done", note=_with_prefilter_note(task, f"[調查結論] {summary[:400]}")
        )
        # 結論沉澱進教訓庫（固定模板但結論各異 → exact_only 防 difflib 近似去重誤殺）。
        with contextlib.suppress(Exception):
            lessons.add_many(
                [f"調查結論（{task['title']}）：{summary[:500]}"],
                session_id=sid,
                requirement=task.get("title") or "",
                source="investigation",
                exact_only=True,
            )
        if parsed["followups"]:
            with contextlib.suppress(Exception):
                _add_discovered_followups(
                    task, parsed["followups"], _pending_titles(), structured=True
                )
        _audit("investigation_done", summary)
        log.info(
            "任務 #%s 調查完成（結論 %d 字、證據 %d 行）",
            task["id"],
            len(summary),
            len(parsed["evidence"]),
        )
        return
    # 缺結論或缺證據（防單專家無憑據自說自話）：走既有「討論未達完成」有限重試語意。
    # 原始輸出頭段必須留痕：調查線事件走 _noop 丟棄（history 0 events），缺結論時若不記
    # 這行,事後完全無從驗屍「到底回了什麼」——2026-07-11 09:24 劣化窗口（SDK 2-4 秒回
    # 垃圾、12 場連環 incomplete、4 任務 attempts 燒光冤死）的診斷就卡在這裡。
    why = "調查輸出缺「結論:」" if not parsed["conclusion"] else "調查結論缺「證據:」行"
    log.warning(
        "任務 #%s 調查輸出無法解析（%s，len=%d）：%.200s",
        task["id"],
        why,
        len(text or ""),
        " ".join((text or "(空)").split()) or "(空)",
    )
    _handle_discussion_incomplete(task, reason=why)


def _screen_followups(items: list, existing_titles: list[str]) -> list:
    """討論回填的後續任務進場前，套與 `_evaluate_self` 相同的品質防線：近期完成去重 +
    良構性/價值閘（`_is_low_value_followup`）+ `_filter_pending_duplicates`（詞集相似度 + 子系統覆蓋廣度）。

    修 discovered 路徑的不對稱（見完成率診斷）：`source="eval"` 的自我發掘走完整 pre-filter，
    但 `run_one_task` 尾端把討論 followup 直接 `add_items`/`add_many`（source="discovered"），
    完全繞過品質閘——「收尾驗收/QA pass/release-e2e-closure」這類 no-op 元任務、與排隊/近期
    已完成高度重疊的提案因此灌爆 backlog（191 pending 在長）。此處是三個 retro emitter 匯流的
    單一 choke point，一次補上即全數涵蓋。

    items 可為結構化 dict（{title, detail?, ...}）或純標題字串；回傳保留原型別、原順序的子集。
    """
    if not items:
        return items

    def _title_of(it) -> str:
        return (it.get("title", "") if isinstance(it, dict) else str(it)).strip()

    def _detail_of(it) -> str:
        return it.get("detail", "") if isinstance(it, dict) else ""

    done = _recent_done_titles()
    fresh = [it for it in items if _title_of(it) and _title_of(it) not in done]
    # 良構性/價值閘（第三道）：丟掉「證據儀式」無會改碼客觀完成判準的自我指涉 meta busywork
    # ——去重防線擋不掉「全新但同樣沒價值」的提案（見完成率第二輪診斷）。
    gated = [it for it in fresh if not _is_low_value_followup(_title_of(it), _detail_of(it))]
    if len(gated) < len(fresh):
        log.info(
            "followup 價值閘丟棄 %d/%d 個低價值 meta 提案（收尾驗收/evidence/落檔 類無改碼判準）",
            len(fresh) - len(gated),
            len(fresh),
        )
    # _filter_pending_duplicates 回傳的是 gated 標題的「保序子集」；以雙指標消費以支援重複標題。
    kept_titles = _filter_pending_duplicates([_title_of(it) for it in gated], existing_titles)
    remaining = list(kept_titles)
    out: list = []
    for it in gated:
        if remaining and remaining[0] == _title_of(it):
            remaining.pop(0)
            out.append(it)
    return out


def _discovered_added_today(now: float | None = None) -> int:
    """今天（UTC）已入列的自產任務數（source=discovered/eval，依 created_at 判日）。

    供每日自產上限（AUTOPILOT_DISCOVERED_DAILY_CAP）計數：pending 172 筆中 85% 是
    系統自產、產生速度 > 消化速度（吞吐 ~8/天），品質閘擋「爛的」、此上限擋「好但
    太多的」——縱橫閘之外的總量閘（第五輪 C2）。
    """
    day = time.gmtime(now if now is not None else time.time())[:3]
    return sum(
        1
        for t in backlog.list_tasks()
        if t.get("source") in ("discovered", "eval")
        and time.gmtime(float(t.get("created_at") or 0))[:3] == day
    )


# SLO 煞車推播的當日一次去重(UTC 日 tuple);行程重啟重置=最多多推一次,可容忍。
_slo_brake_notified_day: tuple | None = None


def _slo_brake_factor() -> int:
    """SLO 自動煞車(第 3 階 A4):信任未達標時自產日額的除數(2=砍半),否則 1。

    把「信任」從人為判斷變成控制迴路:7 天零人工介入合併率(insights.trust_metrics)
    低於 TI_SLO_ZERO_TOUCH_MIN 時,系統自動收縮自產、推播 slo_brake 讓人介入調規範,
    而不是繼續全速產出待人審的工作。0=停用(預設);樣本 < SLO_MIN_MERGED 不煞車
    (冷啟動保護);指標讀取失敗一律不煞車——煞車是加值,不得影響主迴圈。
    """
    threshold = config.SLO_ZERO_TOUCH_MIN
    if threshold <= 0:
        return 1
    try:
        m = insights.trust_metrics(7)
    except Exception:  # noqa: BLE001 — 指標壞了不擋自產
        log.debug("SLO 煞車讀取信任指標失敗(忽略,不煞車)", exc_info=True)
        return 1
    rate = m.get("zero_touch_rate")
    if rate is None or int(m.get("merged") or 0) < config.SLO_MIN_MERGED:
        return 1
    if rate >= threshold:
        return 1
    global _slo_brake_notified_day
    day = time.gmtime()[:3]
    if _slo_brake_notified_day != day:
        _slo_brake_notified_day = day
        notify.send_bg(
            "slo_brake",
            f"零介入合併率 {rate:.0%} 低於門檻 {threshold:.0%}，自產日額自動砍半",
            rate=rate,
            threshold=threshold,
            merged=int(m.get("merged") or 0),
        )
    log.warning(
        "SLO 煞車生效:7 天零介入合併率 %.0f%% < 門檻 %.0f%%,自產日額砍半",
        rate * 100,
        threshold * 100,
    )
    return 2


def _discovered_budget_left(kind: str, want: int) -> int:
    """每日自產上限的剩餘配額（0=旋鈕停用時回 want 不設限）；超額丟棄記 log 留痕。

    SLO 煞車(_slo_brake_factor)生效時上限砍半——僅在上限旋鈕啟用(cap>0)時有意義。
    """
    cap = config.AUTOPILOT_DISCOVERED_DAILY_CAP
    if not cap or want <= 0:
        return want
    cap = max(1, cap // _slo_brake_factor())
    left = max(0, cap - _discovered_added_today())
    if left < want:
        log.info(
            "%s 達每日自產上限（%d/天），丟棄 %d/%d 個提案（明日 UTC 重置）",
            kind,
            cap,
            want - left,
            want,
        )
    return left


def _add_discovered_followups(
    task: dict, raw: list, existing_titles: list[str], *, structured: bool
) -> int:
    """把討論 discovered followup 回填 backlog，套「衍生扇出限制」（完成率修法②）。回傳實際新增數。

    兩道上限與價值閘互補、共同封住 discovered 迴圈灌水（echo chamber）：
    - **血緣代數（縱）**：父任務 gen 已達 `AUTOPILOT_FOLLOWUP_MAX_GEN` → 其 followup 一律不入場（留痕
      丟棄），斷開「followup 生 followup」深鏈。子任務 gen＝父+1，隨 backlog.add 落欄位。
    - **扇出寬度（橫）**：品質防線（`_screen_followups`：近期完成去重 + 價值閘 + 相似度/子系統廣度）後，
      再截斷到 `AUTOPILOT_FOLLOWUP_MAX_PER_TASK`，單一任務一場最多回填這麼多後續。
    兩上限任一為 0＝該維度不限（恢復舊行為）。
    """
    if not raw:
        return 0
    parent_gen = int(task.get("gen", 0) or 0)
    max_gen = config.AUTOPILOT_FOLLOWUP_MAX_GEN
    if max_gen and parent_gen >= max_gen:
        log.info(
            "任務 #%s 已達 followup 血緣代數上限（gen=%d≥%d），丟棄 %d 個後續提案（斷深鏈）",
            task.get("id"),
            parent_gen,
            max_gen,
            len(raw),
        )
        return 0
    items = _screen_followups(raw, existing_titles)
    cap = config.AUTOPILOT_FOLLOWUP_MAX_PER_TASK
    capped = items[:cap] if cap else items
    # 每日自產總量閘（第五輪 C2）：品質/寬度閘之後最後套用，超額當日丟棄。
    capped = capped[: _discovered_budget_left("討論 followup", len(capped))]
    child_gen = parent_gen + 1
    if structured:
        added = backlog.add_items(capped, source="discovered", gen=child_gen)
    else:
        added = backlog.add_many(capped, source="discovered", gen=child_gen)
    log.info(
        "從討論新增 %d 個後續任務（提案 %d、品質過濾後 %d、寬度上限丟棄 %d、gen=%d）",
        added,
        len(raw),
        len(items),
        len(items) - len(capped),
        child_gen,
    )
    return added


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
    # 每日自產總量閘（第五輪 C2）：與討論 followup 共用同一配額。
    tasks = tasks[: _discovered_budget_left("自我評估", len(tasks))]
    n = backlog.add_many(tasks, source="eval")
    # 留痕：兩道進場過濾（done 去重 + pending pre-filter）共丟棄多少提案——讓「源頭擋掉多少瑣碎/重複」
    # 可觀測，而非無聲 log.debug 消失（與 improver._discover 的丟棄留痕對齊）。
    log.info("自我評估產出 %d 個新任務（提案 %d、過濾丟棄 %d）", n, len(raw), len(raw) - len(tasks))
    return n


def _build_split_prompt(task: dict) -> str:
    """組裝「把逾時任務拆小」的 prompt（純字串、無 LLM/網路，可單測）。

    逾時多半＝範圍太大跑不完。要專家把原任務拆成數個**更小、可各自獨立出貨**的子任務,每個都要有
    明確客觀的完成判準,且合起來仍覆蓋原目標。刻意要求「拆到能在一場 session 內做完」以斷開逾時循環。
    """
    title = (task.get("title") or "").strip()
    detail = (task.get("detail") or "").strip()
    n = config.AUTOPILOT_SPLIT_MAX_SUBTASKS
    base = f"原任務標題：{title}\n"
    if detail:
        base += f"原任務細節：{detail}\n"
    return (
        "以下這個任務因為範圍太大、在時間硬牆內跑不完而逾時。請你把它拆解成數個**更小、可各自"
        "獨立出貨**的子任務，讓每一個都能在單一一場工作 session 內完成。\n\n"
        f"{base}\n"
        f"要求：\n"
        f"1. 產出 2～{n} 個子任務，合起來仍覆蓋原任務目標，但各自範圍明顯更小。\n"
        "2. 每個子任務都要有**明確、客觀、可驗證的完成判準**（能改動程式碼並通過測試/lint），"
        "不要產出純驗收/純報告/純落檔這類沒有實質產出的元任務。\n"
        "3. 子任務之間盡量獨立、可分別合併，避免硬相依。\n\n"
        "輸出格式：每行一個子任務，以「任務: 」開頭，例如\n任務: 重構 X 模組的 Y 函式並補單測\n"
    )


# 逾時未拆分 parked note 的固定前綴：note 產生（見下方 _handle_task_timeout）與 backlog.triage_failed
# 的 Rule 1 退回解析、以及本檔 Rule 2 揀選 regex 均引用此常數。此字串被 backlog.triage 跨模組解析，
# 勿自行修改；深度上限變體 note 不含此前綴，天然不被 Rule 1/Rule 2 揀走（交人工）。
_TIMEOUT_NOTE_PREFIX = backlog.TIMEOUT_NOTE_PREFIX
_TIMEOUT_NOTE_RE = re.compile(rf"{re.escape(_TIMEOUT_NOTE_PREFIX)} (\d+)s")


async def _autosplit_task(clone: str, task: dict) -> list[str]:
    """逾時任務（範圍太大）交資深專家拆成更小、可獨立出貨的子任務。回傳過濾後的子任務標題清單。

    子任務只套「良構性/價值閘」（`_is_low_value_followup`）剔除無價值元任務子項，並剔除 parse_tasks 空
    回應 fallback「實作需求」與原任務標題原封回填，再截斷到 `AUTOPILOT_SPLIT_MAX_SUBTASKS`。刻意不走
    `_screen_followups` 全套（其子系統上限/對父任務相似度去重會誤殺合法子任務——拆分本就刻意產出多個
    同子系統、與父任務相近的更小項）。拆不出東西＝回空，交呼叫端退回 parked。純檔案 IO 外殼，LLM 由
    Expert 承擔。
    """
    from .experts import Expert
    from .roles import SENIOR

    sid = f"ap-split-{uuid.uuid4().hex[:8]}"
    ex = Expert(SENIOR, sid, Path(clone))

    async def _noop(_ev):
        return None

    prompt = _build_split_prompt(task)
    try:
        text = await ex.speak(prompt, _noop)
    finally:
        with contextlib.suppress(Exception):
            await ex.stop()
    # 只套「良構性/價值閘」剔除無價值元任務子項，並剔除 parse_tasks 空 fallback「實作需求」與原任務
    # 標題原封回填。刻意**不**走 `_screen_followups` 全套：其子系統覆蓋上限（K）與對父任務的相似度去重
    # 會誤殺合法子任務——拆分本就刻意產出多個同子系統、且與父任務相近的更小項。佇列既有的等值重複交
    # `backlog.add` 的字串等值去重自然擋掉，無須在此重做。
    orig = (task.get("title") or "").strip()
    out: list[str] = []
    for t in parse_tasks(text):
        t = t.strip()
        if not t or t == "實作需求" or t == orig or _is_low_value_followup(t):
            continue
        out.append(t)
    return out[: config.AUTOPILOT_SPLIT_MAX_SUBTASKS]


async def _autosplit_and_enqueue(clone: str, task: dict) -> list[int]:
    """逾時任務交專家拆成更小子任務並入列 pending（`split_depth`＝父 depth+1，封頂由此逐代累計）。
    回傳 child id 清單。child 建立邏輯的**唯一**實作，`_handle_task_timeout`（新逾時）與
    `_maybe_triage_timeout_parked`（Rule 2 歷史 parked 分診）共用，避免雙份 child dict 建立邏輯漂移。
    """
    tid = task["id"]
    depth = int(task.get("split_depth", 0) or 0)
    children: list[int] = []
    for title in await _autosplit_task(clone, task):
        child = backlog.add(
            title,
            detail=f"（由逾時任務 #{tid} 自動拆分，範圍更小以在單場 session 內完成）",
            source="split",
            item_type=task.get("type", "improvement"),
        )
        if child:
            # split_depth 逐代累計，封頂 infinite-split（backlog.add 無此欄位，經 set_status 補寫）。
            backlog.set_status(child["id"], "pending", split_depth=depth + 1)
            children.append(child["id"])
    return children


async def _handle_task_timeout(task: dict) -> None:
    """硬牆逾時任務處理：能自動拆分就拆成更小子任務再排、原任務歸檔 parked；否則維持舊 parked 行為
    （關閉、達拆分深度上限、或拆不出東西/拆分失敗時）。與 `_main_loop` 的 TimeoutError 分支解耦以利單測。

    infinite-split 防護：任務帶 `split_depth`（拆分產物＝父 depth+1）；達 `AUTOPILOT_SPLIT_MAX_DEPTH`
    即不再自動拆。拆分過程任何例外都吞掉並退回 parked——單一任務逾時處理不得弄死主迴圈。
    """
    tid = task["id"]
    depth = int(task.get("split_depth", 0) or 0)
    reached_depth = depth >= config.AUTOPILOT_SPLIT_MAX_DEPTH
    children: list[int] = []
    if config.AUTOPILOT_TIMEOUT_AUTOSPLIT and not config.AUTOPILOT_DRYRUN and not reached_depth:
        try:
            clone = await _prepare_clone()
            children = await _autosplit_and_enqueue(clone, task)
        except Exception:  # noqa: BLE001 — 拆分失敗只退回 parked，不得中斷主迴圈
            log.exception("任務 #%s 逾時自動拆分失敗，退回 parked", tid)

    if children:
        refs = "、".join(f"#{c}" for c in children)
        backlog.set_status(
            tid,
            "parked",
            note=f"逾時（{config.AUTOPILOT_TASK_TIMEOUT}s）已自動拆為 {refs}（原任務歸檔）",
        )
        log.info("任務 #%s 逾時，自動拆為 %d 個子任務：%s", tid, len(children), refs)
    else:
        note = (
            f"逾時且已達自動拆分深度上限（{config.AUTOPILOT_SPLIT_MAX_DEPTH}）——需人工拆分或縮小範圍"
            if reached_depth
            # 此 marker 被 backlog.triage_failed 解析，勿改成另一個字串來源。
            else f"{_TIMEOUT_NOTE_PREFIX} {config.AUTOPILOT_TASK_TIMEOUT}s — 需拆分或縮小範圍"
        )
        backlog.set_status(tid, "parked", note=note)
        log.warning(
            "任務 #%s 逾時（%ss），標 parked（%s）",
            tid,
            config.AUTOPILOT_TASK_TIMEOUT,
            "達深度上限" if reached_depth else "未自動拆分",
        )


# --- PM workflow 分診 -----------------------------------------------------

_TRIAGE_SYSTEM = """你是 Ti 工作室的專案經理（PM），負責在任務開場前選擇本場討論的流程骨架。

三個選項與判準：
- 快速模式：單檔小修、文案/註解/設定調整、測試補強等低風險任務——動態分派→實作→QA 單審，省下三審輪次。
- 動態優先：中等複雜度、需要 PM 運行時溝通與動態分派的任務。
- 預設流程：跨子系統改動、orchestrator/autopilot 核心流程、安全敏感（auth/憑證/發佈）、
  或任何你沒把握的任務——完整三審把關。拿不定主意一律選這個。

只輸出兩行：
理由: <一句話>
流程: <快速模式|動態優先|預設流程>"""


async def _select_workflow(task: dict, clone: str, sid: str) -> tuple[dict | None, str]:
    """任務開場前的 PM workflow 分診：依任務性質選內建流程。

    回 ``(workflow_dict | None, 一句話理由)``；None＝沿用 default_workflow（與現行為
    bit-for-bit 等價）。護欄：
    - 開關 ``config.AUTOPILOT_WORKFLOW_TRIAGE`` 預設關（不發呼叫、零成本）；
    - 白名單只認兩個「內建工廠」（動態優先/快速模式）——刻意不走 workflow.get_workflow()，
      檔案定義可蓋掉保留名，分診絕不能被 workflows.yaml 的同名檔案劫持；
    - complete_once 永不 raise（逾時/離線/LLM 錯誤回空字串）＋本函式整體 try/except
      兜底——任何失敗都收斂回預設流程，絕不影響任務執行。
    """
    if not config.AUTOPILOT_WORKFLOW_TRIAGE:
        return None, ""
    try:
        from . import flow, providers, workflow as workflow_mod

        user = task["title"] + (f"\n\n細節：{task['detail']}" if task.get("detail") else "")
        text = await providers.complete_once(
            _TRIAGE_SYSTEM,
            user,
            session_id=f"{sid}:triage",
            cwd=Path(clone),
            timeout=float(config.AUTOPILOT_TRIAGE_TIMEOUT),
        )
        if not text:
            return None, ""
        name = flow.parse_workflow_choice(text).strip().strip("「」\"'` ")
        reason = flow.parse_triage_reason(text)
        factories = {
            workflow_mod.DYNAMIC_FIRST_NAME: workflow_mod.dynamic_first_workflow,
            workflow_mod.FAST_TRACK_NAME: workflow_mod.fast_track_workflow,
        }
        factory = factories.get(name)
        if factory is None:  # 「預設流程」或未命中白名單 → 走預設（None）
            return None, reason
        return factory(), reason
    except Exception:  # noqa: BLE001 — 分診只是加值，失敗絕不可影響任務執行
        log.exception("workflow 分診失敗（忽略，沿用預設流程）")
        return None, ""


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
    # 質量事件留痕（僅落檔不推播）：信任指標的 gate 失敗計數（events.jsonl）。
    notify.record("gate_failure", gate=gate_label, task_id=task.get("id"), attempt=attempts + 1)
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
        notify.send_bg(
            "task_failed",
            f"任務 #{task['id']} {gate_label} 重試用罄標 failed",
            task_id=task["id"],
            detail=str(task.get("title") or "")[:120],
        )


def _handle_discussion_incomplete(task: dict, reason: str = "") -> None:
    """討論未達完成且不可出貨時的收斂：有限次退回 pending 重試，用罄才永久 failed。

    這是完成率最大的失敗桶（見完成率診斷）。舊行為是單發即永久 failed，但討論未收斂常
    是暫時性的（turn timeout 讓 QA 文字缺通過字樣、provider 抖動、單一 wave flaky、critic
    一時否決；LLM 非決定性，重跑常會過）——值得重試。上限 AUTOPILOT_DISCUSSION_MAX_ATTEMPTS
    （預設 3，第五輪 C1 與客觀閘門對齊——cap=2 實測擋死 48% 的 failed）。計數慣例與 _handle_gate_failure
    一致（讀同一 task["attempts"]、attempts+1 判斷）。note 兩路徑皆保留「討論未達完成」子串，
    讓既有分診（非 infra → 14 天 park）與看板/診斷分類無縫續接。
    """
    attempts = int(task.get("attempts") or 0)
    cap = config.AUTOPILOT_DISCUSSION_MAX_ATTEMPTS
    # 裁決原因（(a)-lite）：PM 判「未完成」時輸出的 `原因: <一句根因>`。附進 note 讓分診
    # 與人工回看有據；「討論未達完成」子串不動，既有分診/看板/診斷分類無縫續接。
    why = f"（原因: {reason.strip()}）" if (reason or "").strip() else ""
    if attempts + 1 < cap:
        fields: dict = {
            "attempts": attempts + 1,
            "note": _with_prefilter_note(task, f"討論未達完成，第 {attempts + 1} 次退回重試{why}"),
        }
        # 重試冷卻：立即重抓會把 attempts 在同一個 provider 劣化窗口內燒光（2026-07-11
        # 09:24 實證:調查線 3 attempts 3 分鐘內用罄、4 任務冤死）。retry_after 由
        # next_pending/claim_next 尊重,把重試錯開到窗口之外。
        if config.AUTOPILOT_RETRY_COOLDOWN_S > 0:
            fields["retry_after"] = time.time() + config.AUTOPILOT_RETRY_COOLDOWN_S
        backlog.set_status(task["id"], "pending", **fields)
        log.info(
            "任務 #%s 討論未達完成，退回 pending 重試（第 %d/%d 次，冷卻 %ds）%s",
            task["id"],
            attempts + 1,
            cap,
            max(0, config.AUTOPILOT_RETRY_COOLDOWN_S),
            why,
        )
    else:
        backlog.set_status(
            task["id"],
            "failed",
            note=_with_prefilter_note(task, f"討論未達完成（連續 {cap} 次未收斂，放棄）{why}"),
        )
        log.info("任務 #%s 討論連續 %d 次未達完成，標 failed 放棄%s", task["id"], cap, why)
        notify.send_bg(
            "task_failed",
            f"任務 #{task['id']} 討論連續 {cap} 次未收斂標 failed",
            task_id=task["id"],
            detail=str(task.get("title") or "")[:120],
        )


# 執行中活動停滯 supervisor 的輪詢/寬限常數（秒）。
_LIVENESS_POLL_S = 30.0  # supervisor 輪詢 events_mtime 的間隔
_STALL_RECLAIM_S = 60.0  # 偵測停滯→取消 session.run 後，等待其收斂的寬限；逾時即放棄續跑


class AutopilotTaskStalled(Exception):
    """任務執行中活動停滯（session events 檔 mtime 長時間未前進，疑似子程序死鎖）。

    刻意**不繼承 TimeoutError**：與硬牆逾時（AUTOPILOT_TASK_TIMEOUT，多半是任務太大跑不完）
    區分處置——停滯是基礎設施型失敗（子程序卡死），標 failed 由分診自動重試；硬牆逾時維持
    parked（需人工拆分）。主迴圈以型別精準分流（見 _main_loop）。
    """


async def _cancel_and_reclaim(run_task: asyncio.Task) -> None:
    """取消 run_task 並在 _STALL_RECLAIM_S 內等它收斂；逾時即放棄（不阻塞主迴圈續跑）。

    Part 1 已把 Expert.stop()/interrupt()/disconnect() 的收尾圈上逾時，正常情況取消會迅速
    收斂；此處的 shield+wait_for 是最後兜底——即使仍有殘留卡點，寬限用罄就放手，讓死鎖至多
    殘留一個 idle 子程序而非拖死整個迴圈。
    """
    run_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(run_task), _STALL_RECLAIM_S)
    except (Exception, asyncio.CancelledError):  # noqa: BLE001 — 收斂或放棄皆吞
        pass


async def _run_session_supervised(session, requirement: str, sid: str):
    """跑 session.run，並在其上疊「硬牆時鐘」與「活動停滯」兩道兜底，讓死鎖就地自癒。

    - session.run 正常完成 → 回傳其結果。
    - 執行時間超過 AUTOPILOT_TASK_TIMEOUT（硬牆，多半任務太大）→ 取消後拋 TimeoutError
      （訊息沿用既有格式，落主迴圈的 parked 分支）。
    - 連續 AUTOPILOT_STALL_TIMEOUT 秒「無任何進展」（疑似子程序死鎖）→ 取消後拋
      AutopilotTaskStalled（落主迴圈的 failed→分診重試分支）。
    - 收到取消（SIGTERM 優雅停機）→ 先回收 run_task 再原樣 re-raise，既有停機收斂不受影響。

    「進展」＝events 檔 mtime 前進 **或** 任一 worker 子程序 CPU tick 前進（沿用 #298 的
    _proc_descendant_cpu／_workers_field）。兩訊號正交互補：events_mtime 在長 inter-message
    間隔會凍結（工具長跑但沒吐串流訊息），此時 cpu_active 仍為 True → 不誤殺合法長任務；反之
    issue #286 的死鎖是子程序卡 ep_poll 零 CPU 且零事件 → 兩訊號同時靜止才判死鎖，訊號更強、
    誤殺風險更低（尤其 TURN_IDLE/HARD 被設 0 停用的 footgun 配置，純 events 會誤殺 CPU 忙碌的
    長 turn）。cpu_active 為 None（非 Linux／無 /proc／首個 tick）時退回純 events 判定，行為與
    未整合前一致。門檻仍取 AUTOPILOT_STALL_TIMEOUT（預設 2400 > TURN_HARD_TIMEOUT）：CPU gate
    是「更難誤殺」，不改動門檻語義（等 API 回應這類零 CPU＋零事件的合法慢 turn 仍靠此餘裕）。
    """
    hard = config.AUTOPILOT_TASK_TIMEOUT or None
    stall = config.AUTOPILOT_STALL_TIMEOUT or None
    loop = asyncio.get_running_loop()
    run_task = asyncio.ensure_future(session.run(requirement))
    started = loop.time()
    last_mtime = history.events_mtime(sid)
    prev_cpu = _proc_descendant_cpu()
    last_progress = started
    try:
        while True:
            done, _ = await asyncio.wait({run_task}, timeout=_LIVENESS_POLL_S)
            if run_task in done:
                return run_task.result()
            now = loop.time()
            # 進展訊號一：events 檔 mtime 前進。
            mtime = history.events_mtime(sid)
            progressed = mtime != last_mtime
            last_mtime = mtime
            # 進展訊號二：worker 子程序 CPU tick 前進（events 凍結時的活性兜底）。
            cur_cpu = _proc_descendant_cpu()
            if _workers_field(prev_cpu, cur_cpu).get("cpu_active") is True:
                progressed = True
            prev_cpu = cur_cpu
            if progressed:
                last_progress = now
            if hard is not None and now - started >= hard:
                await _cancel_and_reclaim(run_task)
                raise TimeoutError(f"autopilot task timeout after {config.AUTOPILOT_TASK_TIMEOUT}s")
            if stall is not None and now - last_progress >= stall:
                await _cancel_and_reclaim(run_task)
                raise AutopilotTaskStalled(
                    f"no activity for {int(now - last_progress)}s"
                    "（events 凍結且 worker 零 CPU，逾時，疑似子程序死鎖）"
                )
    except asyncio.CancelledError:
        await _cancel_and_reclaim(run_task)
        raise


async def run_one_task(task: dict) -> None:
    t0 = time.time()  # 供 audit.jsonl 的 duration_s（整個任務含討論/閘門/合併）
    sid = f"ap{uuid.uuid4().hex[:10]}"
    backlog.set_status(task["id"], "in_progress", session_id=sid)
    log.info("開始任務 #%s：%s（session %s）", task["id"], task["title"], sid)

    clone = await _prepare_clone()
    requirement = task["title"] + (f"\n\n細節：{task['detail']}" if task.get("detail") else "")

    matched_implemented_title = await _prefilter_implemented_match(task, clone)
    if matched_implemented_title:
        matched_note_title = _sanitize_prefilter_title(matched_implemented_title)
        note = f"{_PREFILTER_IMPLEMENTED_NOTE} 疑似已實作，匹配 merged: {matched_note_title}"
        backlog.annotate(
            task["id"],
            note[:500],
            lane=_PREFILTER_IMPLEMENTED_LANE,
        )
        routed_task = {**task, "note": note[:500], "lane": _PREFILTER_IMPLEMENTED_LANE}
        log.info(
            "任務 #%s 命中疑似已實作 prefilter，轉調查分流：%s",
            task["id"],
            matched_note_title,
        )
        await _run_investigation_task(routed_task, clone, sid, t0)
        return

    # 調查分流（完成率第三輪修法一）：調查/驗證型任務走單專家輕量管線，不進多專家
    # session、不過 merge 閘門——其完成判準是結論而非 code diff。所有出口（含例外）
    # 都在 _run_investigation_task 內終局處置（done/parked/升級退回/未收斂重試）。
    if _is_investigation_task(task):
        await _run_investigation_task(task, clone, sid, t0)
        return

    history.start_session(sid, f"[autopilot] {task['title']}")
    turn_state: dict[str, object] = {
        "current_expert": None,
        "turn_started_at": None,
        "last_status_write_at": None,
    }

    async def broadcast(event):
        history.record_event(sid, event.to_dict())
        _refresh_status_for_event(task.get("id"), event, turn_state)

    def clear_turn_status() -> None:
        turn_state["current_expert"] = None
        turn_state["turn_started_at"] = None
        prev_status = _read_status()
        if prev_status.get("state") == "running" and str(prev_status.get("task_id")) == str(
            task.get("id")
        ):
            _write_running_status_preserving(
                task.get("id"),
                last_activity_at=_latest_activity_at(
                    prev_status.get("last_activity_at"), time.time()
                ),
                current_expert=None,
                turn_started_at=None,
            )

    # PM workflow 分診（TI_AUTOPILOT_WORKFLOW_TRIAGE，預設關）：小任務走快速模式省三審、
    # 高風險走預設流程。wf=None＝沿用 default_workflow（現行為）；決策記 log＋session 事件
    # ＋backlog note 三處供稽核（annotate 不動 attempts）。_select_workflow 自身永不 raise，
    # 呼叫端仍兜一層——分診是加值不是依賴，任何失敗都不得擋任務執行（防禦深度）。
    wf, wf_reason = None, ""
    try:
        wf, wf_reason = await _select_workflow(task, clone, sid)
    except Exception:  # noqa: BLE001
        log.exception("workflow 分診呼叫失敗（忽略，沿用預設流程）")
    if wf is not None:
        from . import events as events_mod

        log.info("任務 #%s workflow 分診：%s（%s）", task["id"], wf["name"], wf_reason)
        history.record_event(
            sid,
            events_mod.phase_change(sid, "workflow_triage", f"{wf['name']}｜{wf_reason}").to_dict(),
        )
        with contextlib.suppress(Exception):
            backlog.annotate(task["id"], f"[workflow] {wf['name']}：{wf_reason}")

    session = StudioSession(
        sid,
        broadcast,
        cwd=Path(clone),
        repo_url=f"https://github.com/{config.AUTOPILOT_REPO}",
        workflow=wf,
        # 軟性時間預算＝硬 timeout：session 會在其 SESSION_SOFT_DEADLINE_FRAC 比例處主動收斂、
        # 優雅出貨已完成成果，避免撞 wait_for 硬砍把整場(含已完成任務)全丟成 timeout failed。
        time_budget_s=config.AUTOPILOT_TASK_TIMEOUT or None,
        # session 不自行發佈：autopilot 作為唯一發佈者（_commit_push_merge 等 CI→合併），
        # 否則同一份成果會被 session（ti-studio/<sid>）與 autopilot（autopilot/task-N）各開一個 PR。
        auto_publish=False,
    )
    # 任務中心跳：討論可長達數小時，status.json 若只在揀起時寫一次，外部監控會把
    # 長任務誤判成死鎖。背景任務每分鐘刷新 updated_at＋last_activity_at，涵蓋整個
    # 任務生命週期（討論、閘門、合併、重佈），於本函式收尾時取消。
    heartbeat = asyncio.create_task(_task_heartbeat(task["id"], sid))
    # merge 成敗追蹤：SIGTERM 打斷「已合併但尚未收尾」的任務時，停機收尾必須收斂成
    # done（成果已進 main，退回 pending 重跑只會對同一份成果再開重複 PR），否則才退
    # 回 pending 重排。merge_fields 抓合併當下的追溯欄位（pr/merged_branch）。
    merge_done = False
    merge_fields: dict = {}
    finalized = False

    def _finalize_shutdown() -> None:
        # 停機收尾的單一入口（冪等）：外層 except 與 finally 兜底都可能走到，只做一次。
        nonlocal finalized
        if finalized:
            return
        finalized = True
        _shutdown_finalize_task(task["id"], sid, merged=merge_done, done_fields=merge_fields)

    try:
        try:
            # supervisor 疊「硬牆時鐘（→ TimeoutError → parked）」與「活動停滯（→
            # AutopilotTaskStalled → failed 重試）」兩道兜底，讓執行中死鎖就地自癒，
            # 不再依賴外部監控/人工重啟（issue #286）。硬牆逾時訊息沿用既有格式。
            result = await _run_session_supervised(session, requirement, sid)
        finally:
            # 優雅停機路徑不 finish_session：收尾由停機收斂的 mark_interrupted 處理
            # （error＋停機註記）；這裡照跑會把 meta 先推成 incomplete，蓋掉中斷標記。
            if not _shutdown_requested:
                history.finish_session(sid)
                clear_turn_status()

        # 回饋：討論發現的後續任務寫回 backlog（優先含 priority/type 的結構化版本）。
        # 進場前套與 _evaluate_self 相同的品質防線（近期完成去重 + 相似度/子系統廣度
        # pre-filter），修 discovered 路徑繞過品質閘、灌爆 backlog 的不對稱（見 _screen_followups）。
        existing_titles = _pending_titles()
        if result.get("followup_items"):
            _add_discovered_followups(
                task, result["followup_items"], existing_titles, structured=True
            )
        elif result.get("followups"):
            _add_discovered_followups(task, result["followups"], existing_titles, structured=False)
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
                # 討論未收斂常是暫時性的（LLM 非決定性，重跑常會過）：有限次退回 pending
                # 重試而非單發即永久 failed，用罄才放棄（詳見 _handle_discussion_incomplete）。
                # reason＝PM 驗收判「未完成」時的 `原因:` 裁決根因，附進 note 供分診/回看。
                _handle_discussion_incomplete(task, reason=result.get("incomplete_reason") or "")
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

        # 每日 PR 預算：討論期間可能跨過預算線——merge 前擋下，任務退回 pending 並還原
        # attempts（超預算不是任務的錯，不消耗重試額度、不走 _handle_gate_failure），
        # 跨日後主迴圈自動重跑。
        if not config.AUTOPILOT_DRYRUN and _daily_pr_budget_exceeded():
            backlog.set_status(
                task["id"],
                "pending",
                attempts=int(task.get("attempts") or 0),
                note="每日 PR 預算已滿，UTC 跨日後自動重跑",
            )
            log.warning("任務 #%s 因每日 PR 預算已滿退回 pending，跨日後自動重跑", task["id"])
            return

        # commit / push / squash-merge 進 main
        merge_res = await _commit_push_merge(clone, task)
        merged, msg = merge_res
        no_changes = getattr(merge_res, "no_changes", False)
        auto_pending = getattr(merge_res, "auto_merge_pending", False)
        # 結構化審計：成功與失敗都記（失敗也燒了成本、審計要能回溯）；dryrun 不落檔。
        # pr 非空＝實際開出 PR（計入每日預算）；push 前就被擋（無 PR）→ pr=None，記錄不計數。
        # no_changes 走獨立 outcome，不污染 merge_failed 桶（診斷分類與看板據此分流）。
        if not config.AUTOPILOT_DRYRUN:
            rc_sha, head_sha = await _run(["git", "rev-parse", "HEAD"], cwd=clone, timeout=30)
            _append_audit(
                {
                    "ts": time.time(),
                    "task_id": task.get("id"),
                    "pr": getattr(merge_res, "pr_number", None),
                    "branch": getattr(merge_res, "branch", ""),
                    "head_sha": head_sha.strip() if rc_sha == 0 else "",
                    "outcome": "no_changes"
                    if no_changes
                    else (
                        "merged"
                        if merged
                        else ("merge_pending" if auto_pending else "merge_failed")
                    ),
                    "detail": msg[-400:],
                    "duration_s": round(time.time() - t0, 1),
                    "attempts": int(task.get("attempts") or 0),
                }
            )
        if no_changes:
            # 零 diff＝沒有可出貨的變更（多為收尾驗收/QA 類元任務，本就無事可做）：收斂為
            # parked no-op——不燒重試（省下重跑整場 session）、不落失敗桶（非任務缺陷）。
            backlog.set_status(task["id"], "parked", note="無變更可出貨（no-op，非失敗）")
            log.info("任務 #%s 零 diff，收斂為 parked no-op（不重試）", task["id"])
            return
        if auto_pending:
            # auto-merge 已掛上、PR 留在遠端由 GitHub 背景合併：任務標 merging（非終局，
            # completion_stats 天然排除），autopilot 立即續跑下一場——不阻塞、不關 PR、
            # 不算失敗。reconciler（_maybe_reconcile_open_prs）週期收斂：MERGED→done、
            # BEHIND→update-branch、CI 紅/衝突→關 PR 退回、逾齡→退回重排。audit 已記
            # merge_pending（pr 欄照帶——PR 確實開了，計入每日預算；reconciler 補記的
            # merged 用 pr_ref 欄避免雙計）。
            backlog.set_status(
                task["id"],
                "merging",
                pr=getattr(merge_res, "pr_number", None),
                merged_branch=getattr(merge_res, "branch", ""),
                merge_armed_at=time.time(),
                note="auto-merge 已掛上，待 CI 綠由 GitHub 背景合併",
            )
            log.info(
                "任務 #%s 標 merging（PR #%s auto-merge 背景合併，reconciler 收尾）",
                task["id"],
                getattr(merge_res, "pr_number", None),
            )
            return
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
        # 自此成果已進 main：之後（重佈/收尾）被停機打斷要收斂 done，不得退回重跑。
        merge_done, merge_fields = True, done_fields

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
    except CancelledError:
        # SIGTERM/SIGINT 優雅停機（以旗標區分；wait_for 任務逾時取消的是內層 session.run、
        # 以 TimeoutError 呈現，不會落到這裡）。掛在最外層才涵蓋整個生命週期——閘門、
        # merge/等 CI、重佈這些 20 分鐘級階段被打斷同樣要收尾，否則任務卡死 in_progress
        # 無聲重跑；merge 後被打斷更必須收斂 done（見 _shutdown_finalize_task）。
        if _shutdown_requested:
            _finalize_shutdown()
        raise
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except CancelledError:
            pass  # 心跳自身的取消；外層停機取消若恰在此送達，由下方旗標檢查兜底
        except Exception:  # noqa: BLE001 — 心跳殘留例外絕不可改寫任務結果
            log.debug("心跳任務收尾時拋出例外（忽略，不影響任務結果）", exc_info=True)
        # SIGTERM 競態兜底：停機取消若恰在上面 await 處送達，會與心跳自身的 CancelledError
        # 無法區分而被吞掉；或停機發生在最後一段同步碼、根本沒有 await 可送達取消。
        # 旗標＋cancelling()（取消已被要求）直接判定：補收尾並重新拋出取消，停機絕不遺失。
        cur = asyncio.current_task()
        if _shutdown_requested and cur is not None and cur.cancelling():
            _finalize_shutdown()
            raise CancelledError()
        clear_turn_status()


def _pause(reason: str) -> None:
    with contextlib.suppress(OSError):
        config.AUTOPILOT_PAUSE_FILE.write_text(f"{reason}\n{time.ctime()}\n", encoding="utf-8")
    log.warning("已暫停 autopilot：%s", reason)


# --- 規範迴路(第 3 階 A3) ---------------------------------------------------
# 第 3 階要求人工介入變成 AI 下次讀得到的規範,而不是一次性修正:每 UTC 日一次,把近
# 7 天人工介入筆記(output_review 帶 detail)與失敗事件蒸餾成通用慣例入 lessons
# (source=intervention;模糊去重擋重複)。當日一次去重用行程記憶體:重啟最多多跑一次,
# 由 lessons 去重兜底,可容忍。
_NORMS_SYSTEM = (
    "你是 Ti 的規範蒸餾器。輸入是近 7 天的人工介入筆記與失敗事件。"
    "請萃取「未來能避免同類介入或失敗」的通用執行慣例。規則:\n"
    "- 每條一行,格式「規範: <一句可執行的慣例>」,至多 3 條。\n"
    "- 只寫通用慣例;不寫一次性事實、任務編號、日期。\n"
    "- 材料不足以形成慣例時輸出「無」。"
)
_norms_distill_day: tuple | None = None


def _event_material_line(e: dict) -> str:
    parts = [str(e.get("kind") or "")]
    for key in ("title", "gate", "detail"):
        v = e.get(key)
        if v:
            parts.append(str(v)[:160])
    return "：".join(parts)


async def _maybe_norms_distill() -> None:
    """規範迴路:蒸餾人工介入+失敗事件成慣例(TI_NORMS_LOOP=0 預設關,零成本)。

    FAST 一次呼叫(providers.complete_once,永不 raise);無材料直接跳過;任何失敗
    只 log——規範迴路是加值,不得影響主迴圈。
    """
    global _norms_distill_day
    if not config.NORMS_LOOP:
        return
    day = time.gmtime()[:3]
    if _norms_distill_day == day:
        return
    _norms_distill_day = day
    try:
        from . import interventions, lessons, providers

        notes = [
            f"人工介入（{i.get('kind')}）：{str(i.get('detail'))[:160]}"
            for i in interventions.read_window(7)
            if i.get("category") == "output_review" and i.get("detail")
        ]
        fails = [
            _event_material_line(e)
            for e in notify.read_events(7)
            if e.get("kind") in ("task_failed", "gate_failure")
        ]
        material = "\n".join((notes + fails)[:40])
        if not material:
            return
        text = await providers.complete_once(
            _NORMS_SYSTEM,
            material,
            session_id=f"norms-distill-{day[0]:04d}{day[1]:02d}{day[2]:02d}",
            cwd=Path(config.AUTOPILOT_DEPLOY_DIR),
        )
        norms = [
            ln.split("規範:", 1)[1].strip().lstrip("：: ")
            for ln in (text or "").replace("規範：", "規範:").splitlines()
            if ln.strip().startswith("規範:")
        ]
        norms = [n for n in norms if n][:3]
        if norms:
            added = lessons.add_many(norms, source="intervention")
            log.info("規範迴路：蒸餾出 %d 條、入庫 %d 條（去重後）", len(norms), added)
    except Exception:  # noqa: BLE001 — 規範迴路失敗不得影響主迴圈
        log.warning("規範迴路蒸餾失敗（忽略）", exc_info=True)


# failed 自動分診的頻率護欄：triage_failed 全量掃 backlog（檔案鎖 + 讀寫整份 JSON），
# 每輪迴圈都跑屬多餘 IO；15 分鐘一次已足夠讓基礎設施型失敗及時復活。行程記憶體即可
# （重啟歸零＝重啟後第一輪就跑一次，正合「重啟常因環境修好」的場景）。
_TRIAGE_INTERVAL_S = 900.0
_last_triage_at = 0.0


def _maybe_triage_failed() -> None:
    """每 _TRIAGE_INTERVAL_S 跑一次確定性 failed 分診（backlog.triage_failed）。

    基礎設施型失敗（provider 掛掉／額度 429／網路）的任務原本永遠躺在 failed，
    要人工打 POST /api/autopilot/triage 才復活——主迴圈自動跑，讓環境恢復後任務
    自然回到佇列；陳年失敗同時歸檔 parked。分診只是自癒輔助，失敗不得影響主迴圈。
    """
    global _last_triage_at
    now = time.time()
    if now - _last_triage_at < _TRIAGE_INTERVAL_S:
        return
    _last_triage_at = now
    try:
        stats = backlog.triage_failed()
    except Exception:  # noqa: BLE001 — 分診只是自癒輔助，失敗不得影響主迴圈
        log.exception("failed 自動分診失敗（忽略，不影響主迴圈）")
        return
    if stats.get("retried") or stats.get("parked") or stats.get("revived"):
        log.info(
            "failed 自動分診：%d 筆基礎設施型失敗退回 pending，%d 筆陳年失敗歸檔 parked，"
            "%d 筆討論未收斂冷卻復活",
            stats.get("retried", 0),
            stats.get("parked", 0),
            stats.get("revived", 0),
        )


# Rule 2（歷史 timeout-parked 自動拆分）每輪最多處理筆數：防 backlog 洪水的防爆閥，非營運旋鈕，
# 比照 backlog.TRIAGE_* 用模組常數（不進 config.py，省兩處同步成本）。
_TIMEOUT_SPLIT_PER_ROUND = 1


def _timeout_parked_candidates() -> list[dict]:
    """揀 Rule 2 可自動拆分的 timeout-parked 任務。

    條件（與 Rule 1 互斥、不搶其活）：`status=="parked"` + note 含 `_TIMEOUT_NOTE_PREFIX` +
    **無** `split_done`（未被本規則處理過）+ **Rule 1 不適用**——即「已被 Rule 1 退回重試過
    （`timeout_retried=True`）」或「park 當時的上限 N ≥ 現行 `AUTOPILOT_TASK_TIMEOUT`（調高上限也白搭）」。
    Rule 1 適用者（未重試過且 N < 現行上限＝操作者調高了上限、值得原樣重試）留給 `backlog.triage_failed`
    退回 pending，這裡不搶。深度上限變體 note 不含前綴，天然不入選。
    """
    cur = int(config.AUTOPILOT_TASK_TIMEOUT)
    out: list[dict] = []
    for t in backlog.list_tasks(status="parked"):
        if t.get("split_done"):
            continue
        m = _TIMEOUT_NOTE_RE.search(t.get("note") or "")
        if not m:
            continue
        rule1_applicable = not t.get("timeout_retried") and int(m.group(1)) < cur
        if rule1_applicable:
            continue
        out.append(t)
    return out


async def _maybe_triage_timeout_parked() -> None:
    """Rule 2：每輪挑至多 `_TIMEOUT_SPLIT_PER_ROUND` 筆 Rule 1 退不了的 timeout-parked，交專家自動拆分。

    掛主迴圈頂端（`_maybe_triage_failed` 同位置）。與 Rule 1（確定性退回，無 LLM）分工：這裡走
    `_autosplit_task` 有 LLM 呼叫，故放 async。原任務一律維持 `parked` 並設 `split_done=True`
    ——不論拆成功/拆空/達深度上限/例外，處理過就標記，確保**下輪不再被揀走**（防無限循環的唯一收斂點）。
    整段吞例外：單筆拆分失敗不得弄死主迴圈（同 triage 慣例）。
    """
    if config.AUTOPILOT_DRYRUN or not config.AUTOPILOT_TIMEOUT_AUTOSPLIT:
        return
    try:
        candidates = _timeout_parked_candidates()
    except Exception:  # noqa: BLE001 — 分診只是自癒輔助，失敗不得影響主迴圈
        log.exception("timeout-parked 分診揀選失敗（忽略，不影響主迴圈）")
        return

    for task in candidates[:_TIMEOUT_SPLIT_PER_ROUND]:
        tid = task["id"]
        depth = int(task.get("split_depth", 0) or 0)
        if depth >= config.AUTOPILOT_SPLIT_MAX_DEPTH:
            # 達拆分深度上限：不再自動拆，標 split_done 防重揀，note 導向人工。
            backlog.set_status(
                tid,
                "parked",
                split_done=True,
                note=f"逾時且已達自動拆分深度上限（{config.AUTOPILOT_SPLIT_MAX_DEPTH}）——需人工拆分或縮小範圍",
            )
            log.info("timeout-parked #%s 已達深度上限，標 split_done 待人工", tid)
            continue
        try:
            clone = await _prepare_clone()
            children = await _autosplit_and_enqueue(clone, task)
        except Exception:  # noqa: BLE001 — 單筆拆分失敗只標記，不得中斷主迴圈
            log.exception("timeout-parked 任務 #%s 自動拆分失敗，標 split_done 待人工", tid)
            backlog.set_status(
                tid, "parked", split_done=True, note="逾時任務自動拆分失敗——需人工拆分或縮小範圍"
            )
            continue
        if children:
            refs = "、".join(f"#{c}" for c in children)
            backlog.set_status(
                tid,
                "parked",
                split_done=True,
                note=f"逾時任務已自動拆為 {refs}（原任務歸檔，Rule 2 分診）",
            )
            log.info("timeout-parked #%s 自動拆為 %d 個子任務：%s", tid, len(children), refs)
        else:
            # 拆不出有效子任務（全雜訊/busywork）：標 split_done 防重複打 LLM，導向人工。
            backlog.set_status(
                tid,
                "parked",
                split_done=True,
                note="逾時任務拆不出有效子任務——需人工拆分或縮小範圍",
            )
            log.warning("timeout-parked #%s 拆不出子任務，標 split_done 待人工", tid)


# 任務邊界部署漂移自查的節流/退避（行程記憶體）。節流起點＝行程啟動時刻：首查延後一個
# 完整間隔——重啟多半「因為」剛部署（execv/redeploy），啟動即查必然無 drift 白燒 fetch；
# 也讓短命行程（單元測試等）天然不觸發此「會真的 fetch/reset/restart」的重動作
# （tests/conftest.py 另設 TI_AUTOPILOT_DEPLOY_CHECK_INTERVAL=0 關死，雙保險）。
_last_deploy_check_at = time.time()
_deploy_backoff_until = 0.0


async def _maybe_boundary_redeploy() -> None:
    """任務邊界的部署漂移自查（完成率第三輪修法二A）：解 autodeploy 部署飢餓。

    autodeploy timer 只在「無進行中討論」時 pull+restart，而 autopilot 連續跑任務時討論
    幾乎總在進行——部署窗口極少，已合併的修法長時間「紙上上線」（實測 #369/#370 合併後
    數小時進不了執行碼）；autopilot 的 execv 自我重載又要磁碟碼先變才觸發，雞生蛋。
    此函式掛在主迴圈任務邊界（此刻保證無 autopilot 自己的討論）：節流間隔內 fetch 比對
    origin/<branch>，有 drift 且無「手動」討論（busy_sessions）→ 就地 deploy.redeploy()
    （deploy.lock 已防與 timer 互撞），成功且自身碼有變即走既有 execv 重載序列，讓下一場
    任務直接跑新碼。失敗（redeploy 已自動回滾）→ 退避 AUTOPILOT_DEPLOY_FAIL_BACKOFF ＋
    回填修復任務，**不 _pause**——壞 commit 非本任務產物，暫停會把整個迴圈陪葬；autodeploy
    timer 原邏輯仍在，雙保險。全程 try/except，任何失敗不得弄死主迴圈（同 triage 慣例）。
    """
    global _last_deploy_check_at, _deploy_backoff_until
    if config.AUTOPILOT_DRYRUN or not config.AUTOPILOT_DEPLOY_CHECK_INTERVAL:
        return
    now = time.time()
    if now < _deploy_backoff_until:
        return
    if now - _last_deploy_check_at < config.AUTOPILOT_DEPLOY_CHECK_INTERVAL:
        return
    _last_deploy_check_at = now
    try:
        deploy_dir = str(config.AUTOPILOT_DEPLOY_DIR)
        branch = config.AUTOPILOT_BRANCH
        # deploy_dir 是 origin 單向鏡像；force refspec 避免並行 fetch 的 ref CAS 競爭。
        rc, out = await deploy._run(
            ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=deploy_dir,
            timeout=60,
        )
        if rc != 0:
            log.debug("邊界部署檢查 fetch 失敗（忽略）：%s", out[-200:])
            return
        disk = await deploy.current_head(deploy_dir)
        rc, origin_head = await deploy._run(
            ["git", "rev-parse", f"origin/{branch}"], cwd=deploy_dir, timeout=30
        )
        origin_head = origin_head.strip()
        if rc != 0 or not disk or not origin_head or disk == origin_head:
            return
        # 有手動討論進行中（非 autopilot 的 session）→ 交還 autodeploy timer，不打斷使用者。
        if history.busy_sessions(config.DEPLOY_STALE_AFTER):
            log.info(
                "任務邊界偵測到部署漂移（%s→%s）但有進行中討論，交還 autodeploy timer",
                disk[:8],
                origin_head[:8],
            )
            return
        log.info("任務邊界偵測到部署漂移（%s→%s），就地重佈", disk[:8], origin_head[:8])
        pre_sig = _self_sig()
        ok, dmsg = await deploy.redeploy()
        if not ok:
            _deploy_backoff_until = time.time() + config.AUTOPILOT_DEPLOY_FAIL_BACKOFF
            log.warning(
                "任務邊界重佈失敗（退避 %ds 後再試）：%s",
                config.AUTOPILOT_DEPLOY_FAIL_BACKOFF,
                dmsg,
            )
            with contextlib.suppress(Exception):
                backlog.add("修復導致重佈失敗的 regression", detail=dmsg, source="discovered")
            return
        log.info("任務邊界重佈成功：%s", dmsg)
        # redeploy 已 reset 磁碟碼：自身 studio/*.py 有變即原地 execv（鏡射主迴圈既有重載
        # 序列），下一場任務直接跑新碼；只有 web/靜態變更時不重載（服務端已由 redeploy 重啟）。
        if not config.AUTOPILOT_DRYRUN and _self_sig() != pre_sig:
            log.info("邊界重佈帶入 autopilot 自身程式碼更新，os.execv 重載")
            await _prepare_execv_reload()
            os.execv(sys.executable, [sys.executable, "-m", "studio.autopilot"])
    except Exception:  # noqa: BLE001 — 邊界部署只是自癒輔助，失敗不得影響主迴圈
        log.exception("任務邊界部署檢查失敗（忽略，不影響主迴圈）")


# open PR reconciler 的節流（行程記憶體）。間隔由 config.AUTOPILOT_RECONCILE_INTERVAL_S
# 控制（預設 300，0=停用）；起點 0.0＝行程啟動後第一次檢查就真的跑。2026-07-11 事故教訓：
# 起點原為 time.time()（意在防短命測試行程誤打真 gh），但重啟/execv 都把節流重新起算，
# 疊上「只在任務邊界跑」——邊界 execv 搶在 reconcile 之前重載、新行程又被節流擋掉、下一
# 任務跑數小時無邊界 → reconciler 整晚失能，3 筆 merging 卡 2-8 小時（PR 其實早已合併）。
# 測試安全不靠這裡：tests/conftest.py 設 TI_AUTOPILOT_AUTO_MERGE=0 把入口關死。
_last_reconcile_at = 0.0


def _reconcile_gh_json(out: str) -> dict | list | None:
    """gh --json 輸出的容錯解析：壞 JSON 回 None（呼叫端跳過該筆，不炸 reconciler）。"""
    try:
        return json.loads(out)
    except (TypeError, ValueError):
        return None


def _rollup_has_failure(rollup: list | None) -> bool:
    """statusCheckRollup 是否含實質失敗（FAILURE/TIMED_OUT/CANCELLED）。

    rollup 常含 push＋pull_request 兩組同名 check；任一失敗即視為 CI 紅——寧可保守關 PR
    退回重跑，也不讓紅 PR 掛著 auto-merge 空等到逾齡。空/None＝無法判定，交逾齡兜底。
    """
    for c in rollup or []:
        if str(c.get("conclusion") or "").upper() in ("FAILURE", "TIMED_OUT", "CANCELLED"):
            return True
    return False


async def _maybe_reconcile_open_prs() -> None:
    """每 _RECONCILE_INTERVAL_S 收斂一次 open PR 與 merging 任務（完成率第三輪修法二B）。

    auto-merge 把「等 CI→合併」交還 GitHub 後，任務生命週期多了「merging」懸置態；加上
    中斷殘留的孤兒 PR（歷史缺口：全庫原本沒有任何人回頭認領 open PR）——本函式是兩者的
    唯一收斂點。分診/回收同儕（_maybe_triage_failed / _recover_stale_in_progress）節流
    慣例照抄；全程容錯，gh/網路失敗只 log，絕不弄死主迴圈。
    """
    global _last_reconcile_at
    if config.AUTOPILOT_DRYRUN or not config.AUTOPILOT_AUTO_MERGE:
        return
    if config.AUTOPILOT_RECONCILE_INTERVAL_S <= 0:
        return
    now = time.time()
    if now - _last_reconcile_at < config.AUTOPILOT_RECONCILE_INTERVAL_S:
        return
    _last_reconcile_at = now
    try:
        await _reconcile_open_prs()
    except Exception:  # noqa: BLE001 — reconciler 只是收斂輔助，失敗不得影響主迴圈
        log.exception("open PR reconcile 失敗（忽略，不影響主迴圈）")


async def _reconciler_loop() -> None:
    """open PR reconciler 常駐背景線（第五輪 P1）：收斂不再依賴任務邊界。

    任務邊界的呼叫保留（同一節流，雙入口不重複跑）；本線每 60s 醒來讓
    _maybe_reconcile_open_prs 依間隔自行決定——任務跑數小時期間，merging 任務照樣
    被收斂（MERGED→done、BEHIND→update-branch），不再等到下個邊界（實測 2-8 小時）。
    """
    while True:
        await asyncio.sleep(60)
        try:
            await _maybe_reconcile_open_prs()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 背景線自身失敗不得影響主迴圈
            log.exception("reconciler 背景線檢查失敗（忽略，下輪再試）")


async def _reconcile_open_prs() -> None:
    """兩趟收斂：Pass 1 逐筆核對 merging 任務的 PR 終態；Pass 2 認領/清理孤兒 autopilot PR。"""
    repo = (config.AUTOPILOT_REPO or "").strip()
    if not repo:
        return
    token = publisher.set_repo_override(repo)
    try:
        await _reconcile_merging_tasks(repo)
        await _reconcile_orphan_prs(repo)
    finally:
        publisher.reset_repo_override(token)


async def _reconcile_merging_tasks(repo: str) -> None:
    merging = backlog.list_tasks("merging")
    if merging:
        # pass 級可觀測：沒有這行時「reconciler 有沒有跑」在 journal 完全不可分辨
        # （每筆動作各有 INFO，但全部跳過＝零輸出），2026-07-11 靜默失能即因此難診斷。
        log.info("reconcile：核對 %d 筆 merging 任務", len(merging))
    for task in merging:
        tid = task["id"]
        pr = task.get("pr")
        if not pr:
            backlog.set_status(tid, "pending", note="merging 無 PR 紀錄（異常），退回重排")
            log.warning("任務 #%s merging 但無 PR 紀錄，退回 pending", tid)
            continue
        rc, out = await _run(
            [
                *_GH,
                "pr",
                "view",
                str(pr),
                "-R",
                repo,
                "--json",
                "state,mergeStateStatus,statusCheckRollup",
            ],
            timeout=60,
        )
        data = _reconcile_gh_json(out) if rc == 0 else None
        if not isinstance(data, dict):
            # warning 而非 debug：gh 全掛時整個 pass 靜默失能曾以「零 log 零收斂」呈現
            # （2026-07-11），生產 journal 必須看得見。
            log.warning("reconcile：查 PR #%s 失敗（跳過本輪）：%s", pr, out[-200:])
            continue
        state = str(data.get("state") or "").upper()
        if state == "MERGED":
            # 成果已進 main：補記 audit（用 pr_ref 而非 pr——_todays_pr_count 以 pr 欄計
            # 每日預算，開 PR 當下的 merge_pending 已計過一次，這裡再帶 pr 會雙計）。
            backlog.set_status(tid, "done", note="auto-merge 背景合併完成（reconciler 收斂）")
            _append_audit(
                {
                    "ts": time.time(),
                    "task_id": tid,
                    "pr": None,
                    "pr_ref": pr,
                    "branch": task.get("merged_branch", ""),
                    "head_sha": "",
                    "outcome": "merged",
                    "reconciled": True,
                    "detail": "auto-merge 背景合併完成",
                    "duration_s": None,
                    "attempts": int(task.get("attempts") or 0),
                }
            )
            log.info("任務 #%s 的 PR #%s 已由 auto-merge 合併，收斂 done", tid, pr)
            continue
        if state == "CLOSED":
            _handle_gate_failure(task, "merge", f"PR #{pr} 被外部關閉未合併")
            await _delete_remote_branch(repo, task.get("merged_branch", ""))
            continue
        # OPEN
        ms = str(data.get("mergeStateStatus") or "").upper()
        if ms == "BEHIND":
            # main 保護 strict:true（要求分支與 base 同步）下，auto-merge 會卡 BEHIND 永不
            # 觸發——GitHub 不自動 update。這條 update-branch 是 auto-merge 方案的必要配套：
            # 更新後 CI 重跑、綠了 auto-merge 自然觸發。輪數上限防「main 高頻前進」下無限追。
            rounds = int(task.get("behind_rounds") or 0)
            if rounds >= config.MERGE_BEHIND_RETRIES:
                await _run(
                    [*_GH, "pr", "close", str(pr), "-R", repo, "--delete-branch"], timeout=120
                )
                # 不走 _handle_gate_failure：BEHIND 耗盡是「main 動太快」而非任務缺陷
                # （成品本身沒問題），計 attempts 會讓多 PR 排隊日的無辜任務被推向永久
                # failed（第五輪 C1 誤傷修正）。退回 pending 重排、attempts 原封不動。
                backlog.set_status(
                    tid,
                    "pending",
                    behind_rounds=0,
                    note=f"PR #{pr} 落後 main 追趕 {rounds} 輪仍未合併（main 高頻前進），"
                    "關閉退回重排（不計 attempts）",
                )
                log.info(
                    "任務 #%s 的 PR #%s BEHIND 追趕 %d 輪耗盡，關閉退回 pending（不計 attempts）",
                    tid,
                    pr,
                    rounds,
                )
                continue
            if await publisher._update_branch(int(pr)):
                backlog.set_status(tid, "merging", behind_rounds=rounds + 1)
                log.info(
                    "任務 #%s 的 PR #%s BEHIND，已 update-branch（第 %d 輪）", tid, pr, rounds + 1
                )
            continue
        if ms == "DIRTY" or _rollup_has_failure(data.get("statusCheckRollup")):
            # 真衝突或 CI 紅：auto-merge 永不觸發，關 PR 退回（走既有 gate failure 重試語意）。
            await _run([*_GH, "pr", "close", str(pr), "-R", repo, "--delete-branch"], timeout=120)
            reason = "真衝突（DIRTY）" if ms == "DIRTY" else "CI 失敗"
            _handle_gate_failure(task, "merge", f"PR #{pr} {reason}，已關閉退回")
            continue
        # CI 仍在跑/狀態未收斂：未逾齡就留待下輪；逾齡＝CI 卡死或 runner 出問題，關閉退回
        # （note 帶「逾時」命中 INFRA_FAILURE_RE，triage 可自動重排）。
        armed_at = float(task.get("merge_armed_at") or 0)
        if armed_at and time.time() - armed_at > config.AUTOPILOT_MERGE_MAX_AGE:
            await _run([*_GH, "pr", "close", str(pr), "-R", repo, "--delete-branch"], timeout=120)
            _handle_gate_failure(
                task,
                "merge",
                f"PR #{pr} 等待 CI 逾時（>{config.AUTOPILOT_MERGE_MAX_AGE}s），已關閉退回",
            )


async def _reconcile_orphan_prs(repo: str) -> None:
    """認領/清理孤兒 autopilot PR：開了但沒有任何 merging 任務指向它（中斷殘留）。"""
    rc, out = await _run(
        [*_GH, "pr", "list", "-R", repo, "--state", "open", "--json", "number,headRefName"],
        timeout=60,
    )
    data = _reconcile_gh_json(out) if rc == 0 else None
    if not isinstance(data, list):
        return
    tracked = {str(t.get("pr")) for t in backlog.list_tasks("merging")}
    tasks_by_id = {str(t["id"]): t for t in backlog.list_tasks()}
    for item in data:
        branch = str(item.get("headRefName") or "")
        number = item.get("number")
        m = re.fullmatch(r"autopilot/task-(\d+)", branch)
        if not m or number is None or str(number) in tracked:
            continue
        task = tasks_by_id.get(m.group(1))
        if task is None or task.get("status") in ("done", "parked"):
            # 任務已終局/不存在：PR 是純殘留，關閉清理（順帶解除 ls-remote 認領負擔）。
            await _run(
                [*_GH, "pr", "close", str(number), "-R", repo, "--delete-branch"], timeout=120
            )
            log.info("孤兒 PR #%s（%s）無對應在途任務，已關閉清理", number, branch)
            continue
        if task.get("status") in ("pending", "failed"):
            # 崩潰殘留但成品還在：掛 auto-merge 認領、任務改 merging——不重做整場 session。
            rc, out = await _run(
                [*_GH, "pr", "merge", str(number), "-R", repo, "--auto", "--squash"],
                timeout=60,
            )
            if rc == 0:
                backlog.set_status(
                    task["id"],
                    "merging",
                    pr=number,
                    merged_branch=branch,
                    merge_armed_at=time.time(),
                    note=f"reconciler 認領殘留 PR #{number}（掛 auto-merge）",
                )
                log.info("認領孤兒 PR #%s → 任務 #%s 改 merging", number, task["id"])
            else:
                log.debug("孤兒 PR #%s 掛 auto-merge 失敗（留待下輪）：%s", number, out[-200:])
        # in_progress：任務正在跑，PR 可能是它自己剛開的——不動。


async def _delete_remote_branch(repo: str, branch: str) -> None:
    """盡力刪遠端殘留分支（gh api）；失敗只 log——分支殘留會被 ls-remote 認領路徑兜底。"""
    if not branch:
        return
    rc, out = await _run(
        [*_GH, "api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"], timeout=60
    )
    if rc != 0:
        log.debug("刪除遠端分支 %s 失敗（忽略）：%s", branch, out[-200:])


def _recover_stale_in_progress() -> None:
    """把沒有活躍 history session 的 in_progress 任務放回 pending，並掃除幽靈 running meta。

    autopilot 被 kill、LLM turn 被外部中止、或舊版流程卡在 session.run() 時，backlog 可能
    永久停在 in_progress。busy_sessions 已用 events mtime 做 stale 判定；這裡只負責把
    backlog 狀態拉回可重跑，避免主迴圈永遠看不到這筆任務。

    旁路線當前任務必須跳過：claim_next 只標 in_progress 不蓋 session_id，調查管線又從不
    把 sid 寫回 backlog，對本函式而言整段調查都是「session None」——2026-07-11 灰度首航
    #300 認領後 19ms 即被誤收成 pending 的實證。_sideline_task_info 在認領後同步設定
    （中間無 await），事件迴圈內讀它無競態。豁免帶齡上限：旁路若懸掛（clone 卡住、
    INVESTIGATION_TIMEOUT=0 時 speak 無上限），info 永不清空，無上限豁免會把任務永久
    釘死 in_progress；超齡（2×調查逾時與 3600s 取大，後者對齊 sweep_stale_running 的
    靜默視界）即視同孤兒照收。
    """
    busy = {m.get("session_id") for m in history.busy_sessions(config.DEPLOY_STALE_AFTER)}
    sideline = _sideline_task_info
    exempt_max = max(2 * config.AUTOPILOT_INVESTIGATION_TIMEOUT, 3600)
    for task in backlog.list_tasks("in_progress"):
        if (
            sideline
            and task["id"] == sideline.get("task_id")
            and time.time() - sideline.get("started_at", 0.0) < exempt_max
        ):
            continue
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
    # 幽靈 running meta 掃除：backlog 之外，history 也可能殘留卡在 running 的 meta
    # （restart 殺掉行程、finish_session 沒跑到，且該 sid 早已不在任何 in_progress 任務上）。
    # 每輪迴圈頂端順手治癒，網站不再永遠顯示 ⏳ 執行中。活躍集合帶 busy_sessions 雙保險；
    # 掃除失敗絕不可弄死主迴圈。
    try:
        history.sweep_stale_running(active_sids=frozenset(s for s in busy if s))
    except Exception:  # noqa: BLE001 — 掃除只是自癒輔助，失敗不得影響主迴圈
        log.exception("stale-running 掃除失敗（忽略，不影響主迴圈）")


# --- Claude 訂閱雙帳號自動輪替 --------------------------------------------

# 切換帳號後等 systemd 接手重啟的睡眠秒數：schedule_service_restart 約 1 秒後觸發，
# 本程序會在睡眠中被殺掉重啟；非 systemd 環境重啟不會來，醒來後照常續跑下一輪。
_ROTATE_RESTART_SLEEP = 30.0

# 重複排程防護：已排程輪替重啟後，這段秒數內不再切換/排程（曾發生同一重啟 30 秒內被
# 排兩次）。旗標存行程記憶體——重啟本來就會殺掉本行程，自然歸零，無須持久化。
_ROTATE_RESCHEDULE_GUARD_S = 180.0
_rotate_scheduled_at: float | None = None


def _window_field(rl: dict, window: str, field: str) -> float | None:
    """從帳號 rate_limits 抽單一額度窗（five_hour/seven_day）的數值欄位
    （used_percentage/reset_at）；缺失或非數值回 None。"""
    w = rl.get(window)
    v = w.get(field) if isinstance(w, dict) else None
    return float(v) if isinstance(v, (int, float)) else None


def _claude_accounts_usage(
    snap: dict,
) -> tuple[dict[str, dict[str, float | None]], str | None, dict[str, str]]:
    """從額度快照的 claude 區塊抽 ``(usages, active_label, errors)``，餵給 pick_account。

    usages＝``{label: {"five_hour": 用量%|None, "seven_day": 用量%|None,
    "five_hour_reset": epoch|None, "seven_day_reset": epoch|None}}``（兩窗用量與重置
    時間分開傳，負載＝兩窗取最大、重置優先的邏輯集中在 pick_account）；帳號額度查詢
    異常（error，含非在線帳號的 stale_label）→ 全欄位 None（pick_account 視為不可用、
    不得切入），同時把原始錯誤種類記進 ``errors[label]``——呼叫端據此區分「暫時性查詢
    失敗（unreachable，不該觸發切走）」與「授權壞損（unauthorized/token_missing，仍該
    強制切走）」。無 claude 區塊或無多帳號標籤檔時回 ``({}, None, {})``。
    """
    entry = provider_quota._by_key(snap, "claude") or {}
    usages: dict[str, dict[str, float | None]] = {}
    errors: dict[str, str] = {}
    active: str | None = None
    for acct in entry.get("accounts") or []:
        label = acct.get("label")
        if not isinstance(label, str) or not label:
            continue
        rl = acct.get("rate_limits") or {}
        err = rl.get("error")
        # scoped（Fable 週限）救援用：查詢異常時 models 不可信 → None（本層保守略過）。
        models_usage = rl.get("models") if not err else None
        if err:
            errors[label] = str(err)
            rl = {}  # 查詢異常 → 全欄位 None（不可用）
        usages[label] = {
            "five_hour": _window_field(rl, "five_hour", "used_percentage"),
            "seven_day": _window_field(rl, "seven_day", "used_percentage"),
            "five_hour_reset": _window_field(rl, "five_hour", "reset_at"),
            "seven_day_reset": _window_field(rl, "seven_day", "reset_at"),
        }
        # 只在啟用（CLAUDE_ROTATE_SCOPED）且 PM 有釘 scoped 模型時填 scoped 用量%，餵給
        # pick_account 第 1.5 層；未填＝None＝該層略過（完全相容既有純負載/重置決策）。
        if config.CLAUDE_ROTATE_SCOPED and config.PM_PIN_MODEL:
            usages[label]["scoped"] = claude_accounts.scoped_used_pct(
                config.PM_PIN_MODEL, models_usage
            )
        if acct.get("active"):
            active = label
    return usages, active, errors


def _rotate_log_detail(usages: dict, active: str | None, target: str) -> str:
    """切換 log 的括號內文：``5h 重置 14:30，較 B 早 42 分；7d 重置 07/06 03:00；負載 A 26/B 30``。

    重置時間取目標帳號的 5h 窗（本地 HH:MM）；在線帳號重置較晚時附「較 <在線> 早 N 分」
    （重置優先切換的可讀依據）；目標的 7d 窗重置已知時另附「7d 重置 月/日 時:分」
    （7d 早重置優先切換的可讀依據）。查不到的欄位顯示 ?，不因缺資料炸 log。
    """
    t, a = usages.get(target) or {}, usages.get(active or "") or {}
    rt, ra = t.get("five_hour_reset"), a.get("five_hour_reset")
    reset_txt = f"5h 重置 {time.strftime('%H:%M', time.localtime(rt))}" if rt else "5h 重置 ?"
    if rt and ra and ra - rt >= 60:  # 不足 1 分鐘不標，避免「早 0 分」噪音
        reset_txt += f"，較 {active} 早 {round((ra - rt) / 60)} 分"
    rt7 = t.get("seven_day_reset")
    if rt7:
        reset_txt += f"；7d 重置 {time.strftime('%m/%d %H:%M', time.localtime(rt7))}"

    def f(v: float | None) -> str:
        return "?" if v is None else f"{v:.0f}"

    lt, la = claude_accounts._load(t), claude_accounts._load(a)
    return f"{reset_txt}；負載 {target} {f(lt)}/{active or '?'} {f(la)}"


# 釘選 label 憑證檔缺失的警告節流：同 label 只警告一次，防主迴圈每輪刷 log。
_pin_warn_label: str | None = None


def _maybe_apply_pinned_account() -> str | None:
    """使用者釘選帳號 ≠ 在線時，於任務空檔代為切換＋排程服務重啟，回目標 label；否則 None。

    釘選（手動模式）由 UI 寫 ``claude_accounts`` 的 pin 檔：閒置時 UI 端點直接切換；
    忙碌時 UI 提供「排空後切換」（寫 pin，由本函式在討論空檔代行）——busy 判定沿用自動
    輪替的 ``history.busy_sessions``（活的討論才算，討論空檔即可切，這正是排空要等的時機）。
    刻意獨立於 ``_maybe_rotate_claude_account`` 且主迴圈把本函式排在 pause 檢查「之前」：

    - 排空後切換是使用者顯式指令，不受 ``config.CLAUDE_ROTATE`` 與 quota gate 開關影響
      （輪替只在 gate 區塊內被呼叫，gate 關閉時仍須被執行），且 **autopilot 暫停時亦須
      完成**（暫停停的是取任務、非帳號切換；否則排空切換會永久卡在暫停態）；
    - 不需要額度快照——只讀 pin 檔與在線 label 兩個檔案。

    釘選 label 的憑證檔不存在 → log.warning（同 label 節流一次）並忽略，**不**自動刪
    pin 檔（檔案可能被使用者手動補回；破壞性動作留給人）。任何失敗只留 log，絕不炸
    autopilot 主迴圈。
    """
    global _rotate_scheduled_at, _pin_warn_label
    try:
        pinned = claude_accounts.pinned_label()
        if not pinned:
            return None
        if config.PROVIDER != "claude" or config.has_api_key() or not config.claude_cli_logged_in():
            return None
        if pinned == claude_accounts.active_label():
            return None
        if (
            _rotate_scheduled_at is not None
            and time.time() - _rotate_scheduled_at < _ROTATE_RESCHEDULE_GUARD_S
        ):
            return None
        if not claude_accounts.label_exists(pinned):
            if _pin_warn_label != pinned:
                _pin_warn_label = pinned
                log.warning(
                    "釘選帳號 %s 的憑證檔不存在，忽略釘選（解除釘選或補回憑證檔後恢復）",
                    pinned,
                )
            return None
        running = history.busy_sessions(config.DEPLOY_STALE_AFTER)
        if running:
            log.info("釘選切換：有 %d 場進行中討論，本輪不切換（目標 %s）", len(running), pinned)
            return None
        claude_accounts.switch(pinned)
        deploy.schedule_service_restart()
        _rotate_scheduled_at = time.time()
        log.info("釘選切換：已於任務空檔切至帳號 %s，已排程重啟服務使新憑證生效", pinned)
        return pinned
    except Exception:  # noqa: BLE001 — 釘選代切與輪替同準則,失敗不得弄死主迴圈
        log.exception("釘選帳號切換失敗（忽略，不影響主迴圈）")
        return None


def _maybe_rotate_claude_account(snap: dict) -> str | None:
    """Claude 訂閱雙帳號自動輪替：需要切換時換帳號＋排程服務重啟，回目標 label；否則 None。

    決策純函式在 ``claude_accounts.pick_account``（v4 優先序：95% 安全上限 > 7d 早重置
    多吃（差 ≥ reset_edge_7d 秒）> 5h 早重置多吃（差 ≥ reset_edge 秒）> 負載平均分配
    （差 ≥ margin）；帳號負載＝5h/7d 兩窗取最大，全部達上限交給 quota gate——規則 SSOT
    見其 docstring）；本函式只負責前置防護與副作用：

    - 使用者釘選帳號（``claude_accounts.pinned_label``，手動模式）→ 整段凍結（含
      scoped 救援層）；釘選目標 ≠ 在線的切換由 ``_maybe_apply_pinned_account`` 代行，
      釘選帳號額度耗盡交給既有 quota gate 睡到重置（硬凍結語意：使用者自選）；
    - ``config.CLAUDE_ROTATE`` 關閉、或非「claude 訂閱模式」（provider 非 claude／走
      API key／CLI 未登入）→ 直接不輪替；
    - 已排程輪替重啟未滿 ``_ROTATE_RESCHEDULE_GUARD_S`` 秒 → 不重複切換/排程（同一
      重啟曾被排兩次；重啟殺掉本行程後旗標自然歸零）；
    - 有「真正進行中」的討論不切——重啟會中斷討論，busy 判定鏡射 ti-autodeploy 的
      ``history.busy_sessions(config.DEPLOY_STALE_AFTER)``（stale 的死 session 不算）；
    - 命中 → ``claude_accounts.switch(target)`` 後以 ``deploy.schedule_service_restart()``
      排程重啟 ti.service/ti-autopilot（與 UI 手動切換端點同一 SSOT；SDK 認證在啟動時
      載入記憶體，換檔後須重啟才生效）；
    - 任何失敗只留 log，絕不炸 autopilot 主迴圈。
    """
    global _rotate_scheduled_at
    try:
        if claude_accounts.pinned_label():
            log.debug("帳號輪替：使用者已釘選帳號（手動模式），凍結自動輪替")
            return None
        if not config.CLAUDE_ROTATE:
            return None
        if config.PROVIDER != "claude" or config.has_api_key() or not config.claude_cli_logged_in():
            return None
        if (
            _rotate_scheduled_at is not None
            and time.time() - _rotate_scheduled_at < _ROTATE_RESCHEDULE_GUARD_S
        ):
            log.debug(
                "帳號輪替：%.0f 秒前已排程重啟，跳過重複排程", time.time() - _rotate_scheduled_at
            )
            return None
        usages, active, errors = _claude_accounts_usage(snap)
        # 在線帳號額度「暫時性」查詢失敗（unreachable：429/斷網等）→ 本輪不輪替。
        # 剛切換重啟後行程記憶體快取全失、上游限流可能仍熱：第一次查詢 429 時在線帳號
        # 會映成全 None（不可用），若照舊強制切走就會來回互切＋重啟循環（flap）。下一輪
        # 主迴圈自然重試；授權壞損（unauthorized/token_missing）不在此列，仍強制切走。
        active_err = errors.get(active or "")
        if active_err and active_err not in ("unauthorized", "token_missing"):
            log.info(
                "帳號輪替：在線帳號 %s 額度查詢暫時失敗（%s），本輪不切換、待下輪重查",
                active,
                active_err,
            )
            return None
        target = claude_accounts.pick_account(
            usages,
            active,
            config.CLAUDE_ACCOUNT_PREFERRED,
            config.CLAUDE_ROTATE_THRESHOLD,
            config.CLAUDE_ROTATE_MARGIN,
            config.CLAUDE_ROTATE_RESET_EDGE,
            config.CLAUDE_ROTATE_RESET_EDGE_7D,
            scoped_threshold=config.CLAUDE_SCOPED_LIMIT_THRESHOLD,
        )
        if not target:
            return None
        running = history.busy_sessions(config.DEPLOY_STALE_AFTER)
        if running:
            log.info("帳號輪替：有 %d 場進行中討論，本輪不切換（目標 %s）", len(running), target)
            return None
        claude_accounts.switch(target)
        deploy.schedule_service_restart()
        _rotate_scheduled_at = time.time()
        log.info(
            "Claude 帳號分配：切至 %s（%s），上限 %.0f%%／負載遲滯 %.0f%%／重置優先 5h %.0f 秒"
            "／7d %.0f 秒，已排程重啟服務使新憑證生效",
            target,
            _rotate_log_detail(usages, active, target),
            config.CLAUDE_ROTATE_THRESHOLD,
            config.CLAUDE_ROTATE_MARGIN,
            config.CLAUDE_ROTATE_RESET_EDGE,
            config.CLAUDE_ROTATE_RESET_EDGE_7D,
        )
        return target
    except Exception:  # noqa: BLE001 — 輪替只是額度優化，失敗不得弄死主迴圈
        log.exception("Claude 帳號輪替失敗（忽略，不影響主迴圈）")
        return None


# --- 心跳 ----------------------------------------------------------------


def _quota_summary(snap: dict) -> dict[str, float | None]:
    """把額度快照壓成 ``{provider_key: max_used%}``，供心跳檔與 log 使用（None＝無用量資訊）。"""
    return {
        str(entry.get("key")): provider_quota._usage(entry)["max_used"]
        for entry in snap.get("providers", [])
    }


def _proc_descendant_cpu(
    root_pid: int | None = None, *, proc_root: str = "/proc"
) -> dict[int, int] | None:
    """列舉 root_pid（預設 os.getpid()）的所有後裔子行程，回傳 ``{pid: cpu_ticks}``。

    cpu_ticks = ``/proc/<pid>/stat`` 第 14 欄 utime + 第 15 欄 stime（時鐘 tick；本心跳
    只比較兩次快照的 delta，不換算秒，故不需 ``os.sysconf("SC_CLK_TCK")``）。單趟掃
    ``proc_root`` 下所有數字目錄的 stat：同一趟解析 ppid（第 4 欄）建 親→子 關係並就地
    取 utime/stime，再從 root_pid BFS 展開整棵後裔子樹（**不含 root 自身**——要判定的是
    worker 是否燒 CPU，非主行程；含 claude 子行程再 spawn 的孫行程）。

    選型：掃 ppid map 而非 ``/proc/<pid>/task/<tid>/children``——後者依賴內核
    ``CONFIG_PROC_CHILDREN`` 且並發下不保證完整；ppid 是任何 /proc 恆有的欄位，可攜性最高。

    三態回傳供上層分辨：``dict``（含 0 個以上 pid）＝/proc 可用且掃描成功，``{}`` 明確
    代表「零 worker」；``None``＝/proc 不存在（非 Linux）、無權限、或任何解析失敗——
    子行程取樣純觀測，絕不拋例外弄死心跳。

    comm（第 2 欄）可含空白與括號（如 ``(claude (x))``），故一律以最後一個 ``)`` 之後
    的 token 定位第 3 欄起的欄位（``rpartition(')')``），不可用 ``split()`` 硬切第 2 欄。
    行程可能在 scandir 與 open 之間消失：逐 pid try/except 略過，不中斷整趟掃描。
    """
    try:
        root = os.getpid() if root_pid is None else root_pid
        children: dict[int, list[int]] = {}
        ticks: dict[int, int] = {}
        with os.scandir(proc_root) as it:
            for entry in it:
                if not entry.name.isdigit():
                    continue
                try:
                    with open(
                        os.path.join(proc_root, entry.name, "stat"),
                        encoding="utf-8",
                        errors="replace",
                    ) as fh:
                        raw = fh.read()
                    pid = int(entry.name)
                    rest = raw.rpartition(")")[2].split()  # rest[0]=state(第3欄)…
                    ppid = int(rest[1])  # 第 4 欄
                    utime, stime = int(rest[11]), int(rest[12])  # 第 14、15 欄
                except (OSError, ValueError, IndexError):
                    continue  # 該 pid 消失/壞格式：略過，不弄死整趟
                children.setdefault(ppid, []).append(pid)
                ticks[pid] = utime + stime
        # 從 root BFS 展開所有後裔（不含 root 自身）
        out: dict[int, int] = {}
        stack = list(children.get(root, []))
        while stack:
            pid = stack.pop()
            if pid in out or pid not in ticks:
                continue
            out[pid] = ticks[pid]
            stack.extend(children.get(pid, []))
        return out
    except Exception:  # noqa: BLE001 — 子行程取樣純觀測，任何失敗回 None 不弄死心跳
        return None


def _workers_field(
    prev: dict[int, int] | None, cur: dict[int, int] | None
) -> dict[str, int | bool | None]:
    """把兩次 /proc CPU 快照壓成 status.json 的 ``workers`` 欄位（純函式，好測）。

    count＝當前存活後裔子行程數（cur is None → None）。
    cpu_active＝任一「兩次快照皆存在」的子行程其 CPU tick 前進（True/False）；無法判定
    回 None：cur is None（/proc 不可用）或 prev is None（首個 tick，尚無前次可比）。
    邊界（良性）：worker 於兩 tick 間換 pid 重生 → 無共同 pid → 該窗 cpu_active 記 False；
    60s 窗內 claude 子行程 pid 穩定，與人工「穩定 pid 持續耗 CPU」判活假設一致。
    """
    if cur is None:
        return {"count": None, "cpu_active": None}
    if prev is None:
        return {"count": len(cur), "cpu_active": None}
    active = any(pid in prev and cur[pid] > prev[pid] for pid in cur)
    return {"count": len(cur), "cpu_active": active}


def _write_status(
    state: str,
    *,
    task_id: int | str | None = None,
    sleep_until: float | None = None,
    quota: dict | None = None,
    last_activity_at: float | None = None,
    workers: dict | None = None,
    current_expert: str | None = None,
    turn_started_at: float | None = None,
) -> None:
    """心跳：把當前狀態原子寫入 ``<AUTOPILOT_STATE_DIR>/status.json``。

    state ∈ {"idle", "running", "paused", "quota_sleep", "budget_sleep", "rotate_restart", "stopped"}；
    paused＝pause 檔存在、主迴圈刻意空轉(非卡死,每 10s 刷新 updated_at);
    /api/autopilot 讀此檔回報「迴圈還活著、正在做什麼、睡到何時、各 provider 用量」。
    帳號輪替時 quota 另帶 ``rotated_to``（切換目標 label）；budget_sleep＝每日 PR 預算
    已滿睡到 UTC 跨日；stopped＝收到停機訊號優雅結束（非死鎖）。
    每輪主迴圈寫一次，任務執行中另由 _task_heartbeat 每分鐘刷新，且專家工具/發言事件會
    事件驅動刷新 last_activity_at。current_expert / turn_started_at 記錄目前專家 turn，
    供外部監控與 UI 顯示粒度更細的進度；非任務狀態預設 None。workers＝子行程活性
    （count＝存活後裔數；cpu_active＝任一 worker 自上次 tick 起 CPU tick 前進；None＝/proc
    不可用或首 tick），讓監控能**肯定判定**「有 worker 在燒 CPU＝非死鎖」。寫入走
    secure_write_root（與 backlog 同範式，原子 tmp+rename）；心跳只是輔助觀測，任何寫入
    失敗都不得弄死主迴圈（僅留 debug log）。
    """
    payload = {
        "state": state,
        "task_id": task_id,
        "sleep_until": sleep_until,
        "updated_at": time.time(),
        "quota": quota or {},
        "last_activity_at": last_activity_at,
        "workers": workers,
        "current_expert": current_expert,
        "turn_started_at": turn_started_at,
        "running_commit": _running_commit[:12] or None,
        # 調查旁路線(δ):目前旁路在跑的任務;None=旁路閒置/未啟用。看板據此顯示第二行。
        "sideline": _sideline_task_info,
    }
    try:
        config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        secure_write_root(
            config.AUTOPILOT_STATE_DIR / "status.json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 — 心跳失敗不影響主迴圈
        log.debug("心跳寫入 status.json 失敗（忽略，不影響主迴圈）", exc_info=True)


def _read_status() -> dict:
    """讀回當前 status.json（供任務中心跳保留既有欄位）；讀不到或格式壞回空 dict。

    except 收 ValueError 而非只收 JSONDecodeError：read_text 對壞編碼會拋
    UnicodeDecodeError（ValueError 子類）——心跳背景任務炸掉的例外會在任務收尾 join
    時浮出，絕不可讓「讀個狀態檔」有機會改寫任務結果。
    """
    try:
        data = json.loads((config.AUTOPILOT_STATE_DIR / "status.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# 執行中程式碼的 commit（main() 啟動時擷取一次；execv 重載後自然更新）。
_running_commit = ""

_STATUS_UNSET = object()


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _dict_or_none(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


# 判死規則的睡眠狀態：主迴圈此時阻塞在 asyncio.sleep，updated_at 本就不會每 60s 前進，
# 不可據 updated_at 停滯判死（否則長 quota/budget 睡眠會被誤判主迴圈死）。
_LIVENESS_SLEEP_STATES = frozenset({"quota_sleep", "budget_sleep", "rotate_restart"})


def liveness_verdict(
    status: dict,
    *,
    now: float,
    stale_threshold_s: float,
) -> str:
    """依 `docs/guides/autopilot-monitoring.md` 判定規則 1–5 對 status.json 快照判死。

    範圍誠實聲明：真正執行 restart 的是 **repo 外的「層3監控」腳本**，它並不 import 本函式。
    本謂詞是判死規則的 **repo 內正典實作（reference implementation）**：供回歸守門測試把
    AND 邏輯釘死、並讓外部作者有可對齊的可執行版本，但**不強制**外部監控——外部規則若被
    放寬或餵它的欄位寫壞，本函式仍會綠，那類回歸須靠外部監控自身的測試把關。文件已標示本
    函式為正典並要求外部對齊，`tests/docs/test_qa_task4_liveness_ssot_doc.py` 防兩者漂移。
    定位如此明確，才不會用一個「假 SSOT」製造它本應防止的錯誤信心（issue #285 誤殺教訓）。

    回傳（字串，非例外）：
      - ``"alive"``          仍在工作，**不得** restart。
      - ``"dead_main_loop"`` 規則 1：``updated_at`` 停滯超過門檻＝主迴圈疑似死了。
      - ``"dead_task"``      規則 3 第二條：``running`` 且 ``cpu_active == False`` **且**
                             ``last_activity_at`` 長不動（兩子句同時成立才殺）。

    不變式：
      - null-safe——舊 status.json 缺欄一律當 None，不拋例外。
      - ``current_expert`` / ``turn_started_at`` **完全不參與判死**（規則 5）：長 turn 本就可能
        長時間停在同一專家，據此 restart 會重演誤殺。
      - ``cpu_active`` 為 True 或 ``last_activity_at`` 仍前進，任一為真即 ``alive``（規則 2）。
      - ``cpu_active`` 為 None（/proc 不可用或首 tick）不可單獨判死，退回只看 ``last_activity_at``
        新鮮度（規則 4）。
      - 睡眠狀態（quota/budget/rotate）期間主迴圈阻塞在 sleep，改以 ``sleep_until`` 判是否仍在
        合法睡眠，不因 ``updated_at`` 停滯判死。
    """
    state = _str_or_none(status.get("state"))

    # 規則 1（睡眠特例）：睡眠狀態不看 updated_at，看 sleep_until 是否尚未到期（含門檻裕度）。
    if state in _LIVENESS_SLEEP_STATES:
        sleep_until = _number_or_none(status.get("sleep_until"))
        if sleep_until is not None and now < sleep_until + stale_threshold_s:
            return "alive"
        return "dead_main_loop"

    # 規則 1：updated_at＝主迴圈存活訊號，停滯超過門檻（或缺值）即主迴圈疑似死了。
    updated_at = _number_or_none(status.get("updated_at"))
    if updated_at is None or now - updated_at > stale_threshold_s:
        return "dead_main_loop"

    # 規則 2/3：只有 running 才做任務層判死；其餘（idle/stopped）updated_at 新鮮即存活。
    if state != "running":
        return "alive"

    workers = _dict_or_none(status.get("workers")) or {}
    cpu_active = workers.get("cpu_active")
    last_activity_at = _number_or_none(status.get("last_activity_at"))
    activity_stale = last_activity_at is None or now - last_activity_at > stale_threshold_s

    # 規則 2：cpu_active 為 True（有 worker 燒 CPU）或 last_activity 仍前進 → 不得 restart。
    if cpu_active is True or not activity_stale:
        return "alive"
    # 至此 activity_stale 必為 True。規則 3：cpu_active==False（AND 成立）或
    # 規則 4：cpu_active==None 退回只看 last_activity（已 stale）→ 判死。
    return "dead_task"


def _latest_activity_at(*values: object) -> float | None:
    nums = [_number_or_none(v) for v in values]
    live = [v for v in nums if v is not None]
    return max(live) if live else None


def _write_running_status_preserving(
    task_id: int | str | None,
    *,
    liveness_only: bool = False,
    last_activity_at: object = _STATUS_UNSET,
    workers: object = _STATUS_UNSET,
    current_expert: object = _STATUS_UNSET,
    turn_started_at: object = _STATUS_UNSET,
) -> None:
    """刷新 running 心跳，同時保留主迴圈已寫入的額度/睡眠與其他觀測欄位。

    liveness_only=True＝旁路線的純活性刷新：state/task_id 是主迴圈的身分欄位，旁路
    心跳不得認領——否則看板主任務被蓋成旁路任務（與主迴圈心跳 60s 乒乓，2026-07-11
    生產實測 main 顯示成 sideline 的 #457），主迴圈的 quota_sleep/paused 也會被蓋回
    running。旁路任務自己的顯示走 sideline 子欄（_write_status 每次寫入自動帶
    _sideline_task_info），不經這兩個欄位。
    """
    prev = _read_status()
    state = "running"
    if liveness_only:
        task_id = prev.get("task_id")
        state = str(prev.get("state") or "running")
    prev_quota = prev.get("quota")
    resolved_last_activity = (
        _number_or_none(prev.get("last_activity_at"))
        if last_activity_at is _STATUS_UNSET
        else _number_or_none(last_activity_at)
    )
    resolved_workers = (
        _dict_or_none(prev.get("workers")) if workers is _STATUS_UNSET else _dict_or_none(workers)
    )
    resolved_current_expert = (
        _str_or_none(prev.get("current_expert"))
        if current_expert is _STATUS_UNSET
        else _str_or_none(current_expert)
    )
    resolved_turn_started_at = (
        _number_or_none(prev.get("turn_started_at"))
        if turn_started_at is _STATUS_UNSET
        else _number_or_none(turn_started_at)
    )
    _write_status(
        state,
        task_id=task_id,
        sleep_until=_number_or_none(prev.get("sleep_until")),
        quota=prev_quota if isinstance(prev_quota, dict) else None,
        last_activity_at=resolved_last_activity,
        workers=resolved_workers,
        current_expert=resolved_current_expert,
        turn_started_at=resolved_turn_started_at,
    )


def _event_type_value(event: object) -> str:
    typ = getattr(event, "type", "")
    return str(getattr(typ, "value", typ))


def _event_payload(event: object) -> dict:
    payload = getattr(event, "payload", {})
    return payload if isinstance(payload, dict) else {}


def _event_speaker_key(payload: dict) -> str | None:
    for key in ("speaker", "speaker_key", "role"):
        speaker = _str_or_none(payload.get(key))
        if speaker is not None:
            return speaker
    return None


_EVENT_STATUS_WRITE_MIN_INTERVAL_S = 1.0


def _refresh_status_for_event(
    task_id: int | str | None,
    event: object,
    turn_state: dict[str, object],
) -> None:
    """工具使用與發言完成時，事件驅動刷新 activity 與目前專家 turn。"""
    typ = _event_type_value(event)
    payload = _event_payload(event)
    is_tool = typ == events.EventType.TOOL_USE.value
    is_message = typ == events.EventType.EXPERT_MESSAGE.value
    if not is_tool and not is_message:
        return
    if is_message and payload.get("streaming") and not payload.get("final"):
        return

    now = time.time()
    speaker = _event_speaker_key(payload)
    current = _str_or_none(turn_state.get("current_expert"))
    new_turn = speaker is not None and speaker != current
    if new_turn:
        current = speaker
        turn_state["current_expert"] = speaker
        turn_state["turn_started_at"] = now
    else:
        last_write = _number_or_none(turn_state.get("last_status_write_at"))
        if last_write is not None and now - last_write < _EVENT_STATUS_WRITE_MIN_INTERVAL_S:
            return

    turn_state["last_status_write_at"] = now
    _write_running_status_preserving(
        task_id,
        last_activity_at=now,
        current_expert=current,
        turn_started_at=turn_state.get("turn_started_at"),
    )


# 任務中心跳的刷新間隔（秒）。status.json 原本只在任務揀起時寫一次，任務一超過外部監控
# 的 stale 門檻（如 45 分鐘）就被誤判死鎖；每分鐘刷新從源頭消滅這種假 stale。
_HEARTBEAT_INTERVAL_S = 60.0


async def _task_heartbeat(
    task_id: int | str | None, sid: str, *, liveness_only: bool = False
) -> None:
    """任務執行期間的背景心跳：每 ~60 秒刷新 status.json 的 updated_at 與 last_activity_at。

    last_activity_at 取既有事件驅動值與當前 session events 檔 mtime 的較新者，避免 60s tick
    把較新的工具/發言活動時間倒寫回舊 mtime。另每 tick 取 os.getpid() 後裔子行程 CPU 快照，
    跨兩 tick 比較 delta 寫入 workers（count/cpu_active），讓監控在長 inter-message 間隔
    （events mtime 凍結）仍能肯定「有 worker 燒 CPU＝非死鎖」；取樣失敗回 None，絕不影響
    任務。既有欄位（sleep_until/quota/current_expert/turn_started_at）自 status.json 讀回
    保留，寫入仍走 _write_status 單一 choke point。由 run_one_task 啟動、finally 取消；寫入
    失敗由 _write_status 自行吞掉。liveness_only=True＝旁路線模式：不認領 state/task_id
    （見 _write_running_status_preserving）。
    """
    prev_cpu: dict[int, int] | None = None
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        cur_cpu = _proc_descendant_cpu()
        workers = _workers_field(prev_cpu, cur_cpu)
        prev = _read_status()
        _write_running_status_preserving(
            task_id=task_id,
            liveness_only=liveness_only,
            last_activity_at=_latest_activity_at(
                prev.get("last_activity_at"), history.events_mtime(sid)
            ),
            workers=workers,
        )
        prev_cpu = cur_cpu


# --- 優雅停機（SIGTERM/SIGINT） -------------------------------------------

# 停機旗標：signal handler 設起。用來把「主迴圈任務被取消」區分成兩種——優雅停機
# （SIGTERM/SIGINT，任務退回 pending 自動重排）vs 其他取消（如 wait_for 任務逾時，
# 該路徑取消的是內層 session.run、以 TimeoutError 呈現，不會誤入停機分支）。
_shutdown_requested = False


def _request_shutdown(sig_name: str, main_task: asyncio.Task | None) -> None:
    """SIGTERM/SIGINT handler：設停機旗標後取消主迴圈任務，觸發優雅收尾。

    重複訊號刻意「不去重」：首次取消在極端競態下可能被吞（恰於心跳 join 送達、或停機
    落在無 await 的同步長路徑），重複的 systemd／人工訊號必須能再度 cancel 觸發停機。
    """
    global _shutdown_requested
    _shutdown_requested = True
    log.warning("收到 %s，優雅停機：中斷當前工作、任務退回 pending 自動重排", sig_name)
    if main_task is not None and not main_task.done():
        main_task.cancel()


def _install_signal_handlers() -> None:
    """在主迴圈啟動時掛 SIGTERM/SIGINT 的優雅停機 handler。

    沒有這層時 systemctl restart 直接 SIGTERM 殺死行程：in-flight 任務卡死 in_progress、
    history meta 永遠 running、重跑從零開始。非 Unix 事件迴圈（add_signal_handler 不支援）
    時靜默略過，維持舊行為。
    """
    # 區域 import 取真 asyncio：部分主迴圈測試會把模組級 asyncio 換成 stub（只帶
    # sleep/to_thread），這裡必須拿到真模組才能取得事件迴圈與當前任務。
    import asyncio as aio

    loop = aio.get_running_loop()
    main_task = aio.current_task()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(
                signum, _request_shutdown, signal.Signals(signum).name, main_task
            )


def _set_status_if_in_progress(task_id: int | str, status: str, **fields) -> bool:
    """僅當任務仍為 in_progress 才改寫狀態，回傳是否有寫。

    停機收尾的冪等護欄：任務可能在被打斷前已寫下最終結果（閘門重試的 pending、放棄的
    failed、重佈失敗的 failed、正常完成的 done）——停機收尾絕不可覆蓋既定結果，只救
    「還掛在 in_progress」的任務。
    """
    cur = next((t for t in backlog.list_tasks() if t.get("id") == task_id), None)
    if cur is None or cur.get("status") != "in_progress":
        return False
    backlog.set_status(task_id, status, **fields)
    return True


def _graceful_shutdown_cleanup(task_id: int | str, sid: str | None) -> None:
    """優雅停機打斷「尚未合併」任務時的收尾（全同步 IO，遠低於 systemd 預設 90s stop timeout）：

    - backlog：仍在 in_progress 的任務退回 pending（附註記）——服務重啟後自動重排，
      不再無聲從零重跑；已寫下最終結果者不動（_set_status_if_in_progress 護欄）；
    - history：running meta 標 error（mark_interrupted，冪等）——網站不再永遠顯示 ⏳ 執行中；
    - status.json：state="stopped"——供 /api/autopilot 與外部監控辨識「主動停機」而非死鎖。
    各步驟獨立容錯，單步失敗不得阻斷其餘收尾；整體冪等，可安全重入。
    """
    note = "服務重啟中斷，自動重排"
    try:
        _set_status_if_in_progress(task_id, "pending", note=note)
    except Exception:  # noqa: BLE001 — 收尾單步失敗不得阻斷其餘步驟
        log.exception("停機收尾：任務 #%s 退回 pending 失敗", task_id)
    try:
        if sid:
            history.mark_interrupted(sid, note)
    except Exception:  # noqa: BLE001
        log.exception("停機收尾：session %s 標記中斷失敗", sid)
    _write_status("stopped", task_id=task_id)
    log.warning("停機收尾完成：任務 #%s 已退回 pending（session %s 標記中斷）", task_id, sid)


def _shutdown_finalize_task(
    task_id: int | str, sid: str | None, *, merged: bool, done_fields: dict | None = None
) -> None:
    """優雅停機打斷任務的收尾總入口（merge 感知、冪等）：

    - merge 已成功（PR 已進 main）→ 收斂 done（帶 pr/merged_branch 追溯欄位）——絕不
      退回 pending：成果已合併，重跑只會對同一份成果再開重複 PR、燒掉整輪額度；
    - 尚未 merge → 退回 pending 自動重排（走 _graceful_shutdown_cleanup 原語意）。
    兩路皆 mark_interrupted（冪等，只動 running meta）＋ status.json state="stopped"。
    """
    if not merged:
        _graceful_shutdown_cleanup(task_id, sid)
        return
    note = "服務重啟中斷於合併後——成果已進 main，收斂為 done"
    try:
        if _set_status_if_in_progress(task_id, "done", note=note, **(done_fields or {})):
            log.warning("停機收尾：任務 #%s 已合併，收斂為 done（不重跑）", task_id)
    except Exception:  # noqa: BLE001 — 收尾單步失敗不得阻斷其餘步驟
        log.exception("停機收尾：任務 #%s 收斂 done 失敗", task_id)
    try:
        if sid:
            history.mark_interrupted(sid, note)
    except Exception:  # noqa: BLE001
        log.exception("停機收尾：session %s 標記中斷失敗", sid)
    _write_status("stopped", task_id=task_id)


# --- 主迴圈 --------------------------------------------------------------


async def _prepare_execv_reload() -> None:
    """os.execv 自我重載前的訊號安全準備。

    execv 原地替換行程映像：事件迴圈裡「已排入但尚未執行」的 SIGTERM callback 會被無聲
    丟棄——systemd 的停止請求消失，90 秒後被 SIGKILL 硬殺新映像。兩步防護：先卸下
    SIGTERM/SIGINT handler（回復預設處置：晚到的訊號直接終止行程，正合 systemd 預期），
    再讓出一個 tick 讓「已排入」的 _request_shutdown 跑完——其 cancel 會以 CancelledError
    中止 execv 路徑，改走優雅停機。
    """
    # 區域 import 取真 asyncio：模組級 asyncio 可能被主迴圈測試 stub 掉。
    import asyncio as aio

    # execv 不會回來,main 的 finally 不會跑——旁路調查的收尾必須在這裡做。
    _requeue_sideline_task("execv 重載")
    loop = aio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(signum)
    await asyncio.sleep(0.1)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("autopilot 啟動（dryrun=%s, repo=%s）", config.AUTOPILOT_DRYRUN, config.AUTOPILOT_REPO)
    # Type=notify 啟動握手:不發 READY systemd 會判啟動失敗,故放 main 最早期、任何可能
    # 耗時的步驟(git/部署檢查)之前。execv 自我重載後 NOTIFY_SOCKET 保留,會再發一次(無害)。
    _sd_notify("READY=1")
    # 啟動時擷取一次「執行中程式碼」的 commit（磁碟 HEAD 可能已被 reset 但行程未重載，
    # 兩者語意不同）；隨 status.json 供 /api/autopilot 顯示部署漂移。失敗留空不擋啟動。
    global _running_commit
    with contextlib.suppress(Exception):
        _running_commit = await deploy.current_head(str(config.AUTOPILOT_DEPLOY_DIR))
    startup_sig = _self_sig()
    _install_signal_handlers()

    try:
        # 加值監督,建不起來不擋啟動(既有 main-loop 測試以 SimpleNamespace stub 本模組的
        # asyncio、只給 sleep——monitor 對它們是雜訊,缺 create_task 即靜默跳過)。
        monitor = asyncio.create_task(_loop_monitor())
        sideline = asyncio.create_task(_investigation_sideline())
        notifier = asyncio.create_task(_watchdog_notifier())
        reconciler = asyncio.create_task(_reconciler_loop())
        digester = asyncio.create_task(_digest_scheduler())
    except AttributeError:
        monitor = None
        sideline = None
        notifier = None
        reconciler = None
        digester = None
    try:
        await _main_loop(startup_sig)
    except CancelledError:
        if not _shutdown_requested:
            raise
        # 優雅停機：in-flight 任務已由 run_one_task 的取消分支收尾（退 pending＋標中斷
        # ＋state="stopped"）；idle／睡眠中被取消則在此補寫最終心跳。全程同步 IO，
        # 遠低於 systemd 預設 90s stop timeout，即刻結束行程。
        if _read_status().get("state") != "stopped":
            _write_status("stopped")
        log.warning("autopilot 已優雅停機（任務已重排，服務重啟後自動續跑）")
    finally:
        # 先收尾旁路調查再 cancel:sideline 的 finally 會清掉 _sideline_task_info。
        _requeue_sideline_task("優雅停機")
        for aux in (monitor, sideline, notifier, reconciler, digester):
            if aux is not None:
                aux.cancel()
                with contextlib.suppress(BaseException):
                    await aux


# 暫停狀態轉換旗標(行程記憶體):只在「進入/離開暫停」時各記一次 log,避免每 10s 刷屏。
_paused_logged = False

# 額度全受限通知旗標(行程記憶體):每個受限期只發一次 webhook(F2),恢復 usable 即重置。
_quota_notified = False

# 主迴圈心跳(穩定強化 β):每輪頂端/任務返回後更新。stall/hard 看門狗只包 session.run,
# 「任務之間」(quota snapshot/reconciler/邊界部署/triage)是盲區——任一步無聲卡死即整台
# 停擺且無 log(2026-07-10 盲區調查)。_loop_monitor 據此告警;自救交 systemd watchdog(γ)。
_loop_tick_at = time.time()
_task_running = False


def _loop_tick() -> None:
    global _loop_tick_at
    _loop_tick_at = time.time()


async def _loop_monitor() -> None:
    """主迴圈心跳監督(告警不自殺):非暫停、非任務執行中,且 tick 逾
    TI_AUTOPILOT_LOOP_STALL_S 未推進 → log.error(每個停滯期只吼一次)。"""
    alerted = False
    while True:
        await asyncio.sleep(60)
        try:
            if not config.AUTOPILOT_LOOP_STALL_S:
                continue
            idle_for = time.time() - _loop_tick_at
            stalled = (
                idle_for > config.AUTOPILOT_LOOP_STALL_S
                and not _task_running
                and not config.autopilot_paused()
            )
            if stalled and not alerted:
                log.error(
                    "主迴圈心跳停滯 %.0fs(>%ds):任務之間的某一步(quota/reconcile/部署檢查)"
                    "疑似無聲卡死——等待 systemd watchdog 或人工介入",
                    idle_for,
                    config.AUTOPILOT_LOOP_STALL_S,
                )
                notify.send_bg(
                    "loop_stall",
                    f"主迴圈心跳停滯 {idle_for:.0f}s，疑似無聲卡死",
                    idle_for=round(idle_for),
                )
                alerted = True
            elif not stalled:
                alerted = False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 監督器自身失敗不得影響主迴圈
            log.debug("loop monitor 檢查失敗(忽略)", exc_info=True)


# systemd watchdog 對接(穩定強化 γ):β 的 loop monitor 只告警不自殺,真正自救交
# systemd——unit 換 Type=notify+WatchdogSec=300,行程每 60s 送 WATCHDOG=1,連續漏
# 5 次(整個行程無聲凍結,含 event loop 卡死)即被 systemd 殺掉再 Restart=always 拉起。
_WATCHDOG_PING_S = 60


def _sd_notify(msg: str) -> None:
    """零依賴 sd_notify:往 NOTIFY_SOCKET(unix datagram)送一則通知。

    非 systemd 環境(測試/手動執行)無此環境變數 → 靜默 no-op;socket 任何失敗也
    只 debug log 不冒泡——通知是加值,絕不影響主迴圈。@ 開頭為 abstract socket。
    """
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        log.debug("sd_notify 送出失敗(忽略):%s", msg, exc_info=True)


async def _watchdog_notifier() -> None:
    """每 60s 送 WATCHDOG=1。**暫停中也送**——paused 是活著,不該被 systemd 誤殺;
    「假活」(event loop 卡死)本 task 也動不了,ping 自然斷,正是 watchdog 要抓的。"""
    while True:
        await asyncio.sleep(_WATCHDOG_PING_S)
        _sd_notify("WATCHDOG=1")


async def _digest_scheduler() -> None:
    """digest 每日落盤（第五輪 F6）：當日（UTC）檔不存在即產出寫入，不再「關掉面板即失」。

    純本地模板（零 LLM、零網路），每小時醒來檢查一次即可；同日重寫冪等。任何失敗
    log 後下輪再試，絕不影響主迴圈。
    """
    while True:
        try:
            existing = {d["name"] for d in await asyncio.to_thread(digest.list_digests)}
            today = f"digest-{time.strftime('%Y-%m-%d', time.gmtime())}.md"
            if today not in existing:
                name = await asyncio.to_thread(digest.save_digest)
                log.info("digest 已落盤：%s", name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 排程器自身失敗不得影響主迴圈
            log.debug("digest 落盤失敗（下輪再試）", exc_info=True)
        await asyncio.sleep(3600)


async def _pause_tick() -> None:
    """暫停中的一輪空轉——必須可觀測(2026-07-10 事故:pause 後 status.json 凍結在
    上一筆 running #293,53 分鐘死寂被誤判成「看門狗失效的卡死」並觸發人工重啟)。

    進入暫停的第一輪:記 log+收斂殘留 in_progress(看板不停在假 running);每輪
    `_write_status("paused")` 刷新 updated_at——外部監控據此區分「刻意暫停」與「真卡死」。
    """
    global _paused_logged
    if not _paused_logged:
        log.info("autopilot 已暫停(pause 檔存在),主迴圈空轉待恢復")
        _paused_logged = True
        with contextlib.suppress(Exception):
            _recover_stale_in_progress()
    _write_status("paused")
    await asyncio.sleep(10)


def _note_resumed() -> None:
    """離開暫停時記一次 log(冪等;非暫停期間為 no-op)。"""
    global _paused_logged
    if _paused_logged:
        log.info("autopilot 已恢復(pause 檔移除),繼續取任務")
        _paused_logged = False


# 調查旁路線的狀態(行程記憶體):目前在跑的旁路任務(供 status.json sideline 子欄)。
_sideline_task_info: dict | None = None


def _requeue_sideline_task(reason: str) -> None:
    """execv 重載/優雅停機腰斬旁路調查前,把它顯式退回 pending 並退還本輪 attempts。

    腰斬本身是既有設計(部署不等調查,stale reaper 兜底);但不收尾的成本有二:任務
    留 in_progress 等 reaper(至多 DEPLOY_STALE_AFTER 延遲),且 claim_next 的
    attempts+1 白燒——重載一天數次、旁路近全時忙碌,同一任務被斬幾次就會被錯誤
    parked(2026-07-11 08:46 execv 腰斬 #490 實證)。冪等:旁路已寫終局(done/parked/
    pending)則 no-op;必須在 cancel 旁路 task 之前呼叫(其 finally 會清 info)。
    任何失敗吞掉交 reaper 兜底,絕不影響重載/停機路徑。
    """
    info = _sideline_task_info
    if not info:
        return
    tid = info.get("task_id")
    try:
        cur = next((t for t in backlog.list_tasks("in_progress") if t["id"] == tid), None)
        if cur is None:
            return
        backlog.set_status(
            tid,
            "pending",
            attempts=max(0, int(cur.get("attempts") or 1) - 1),
            note=f"{reason}腰斬旁路調查,退回重排(退還本輪 attempts)",
        )
        log.warning("%s:旁路調查任務 #%s 退回 pending(attempts 退還)", reason, tid)
    except Exception:  # noqa: BLE001 — 收尾失敗交既有 stale reaper 兜底,不得影響呼叫路徑
        log.exception("旁路任務 #%s 退回 pending 失敗(交 stale reaper 兜底)", tid)


async def _investigation_sideline() -> None:
    """調查任務旁路併行線(吞吐強化 δ,預設關):主 worker 跑完整管線(~51min/場)時,
    本線併行消化調查分流任務(~89s/筆;live 量測 pending 37% 符合)。

    設計邊界:單線一次一筆(+1 LLM 併發);與主迴圈共用 pause/quota 閘門(暫停或額度
    受限即不取);認領走 backlog.claim_next(單一 flock 內 filter+標 in_progress,消
    與主迴圈的 TOCTOU);clone 用獨立唯讀目錄(AUTOPILOT_WORK_DIR+"-inv")避免主 worker
    reset --hard 抽換檔案。任何例外 log+continue,絕不影響主迴圈;停機隨 main cancel,
    in_progress 由既有 stale recovery 收斂。
    """
    global _sideline_task_info
    inv_dir = str(config.AUTOPILOT_WORK_DIR) + "-inv"
    while True:
        await asyncio.sleep(60)
        try:
            if not config.AUTOPILOT_INVESTIGATION_PARALLEL:
                continue
            if not config.AUTOPILOT_INVESTIGATION_LANE:
                continue
            if config.autopilot_paused() or _shutdown_requested:
                continue
            # 額度閘門:與主迴圈同一判定(provider_quota.gate);snapshot 有 SWR 快取,
            # 60s 一次的旁路輪詢幾乎都命中快取、不重打 API。全受限即不取任務。
            if config.AUTOPILOT_QUOTA_GATE:
                try:
                    snap = await asyncio.to_thread(provider_quota.snapshot)
                    usable, _reset = provider_quota.gate(snap)
                    if not usable:
                        continue
                except Exception:  # noqa: BLE001 — 額度查詢失敗寧可保守跳過本輪
                    continue
            task = backlog.claim_next(_is_investigation_task)
            if task is None:
                continue
            log.info("旁路線認領調查任務 #%s:%s", task["id"], task["title"][:60])
            t0 = time.time()
            sid = f"apinv{uuid.uuid4().hex[:8]}"
            _sideline_task_info = {
                "task_id": task["id"],
                "title": task["title"][:80],
                "started_at": t0,
            }
            try:
                clone = await _prepare_clone(inv_dir)
                await _run_investigation_task(task, clone, sid, t0, sideline=True)
            finally:
                _sideline_task_info = None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 旁路線絕不影響主迴圈
            log.exception("調查旁路線本輪失敗(忽略,60s 後重試)")


async def _main_loop(startup_sig: float) -> None:
    while True:
        # 停機旗標兜底：取消若在某處被吞（競態）而迴圈還在轉，這裡立即補上停機路徑，
        # 絕不再取新任務（否則要等 systemd 90s 後 SIGKILL 硬殺）。
        if _shutdown_requested:
            raise CancelledError()
        _loop_tick()

        # 使用者釘選帳號（手動模式）≠ 在線 → 於討論空檔代為切換。刻意放在 pause 檢查
        # 「之前」：排空後切換是使用者顯式指令，即使 autopilot 暫停也該完成（切完重啟、
        # pause 檔仍在→續暫停，語意一致：pause 停的是「取任務」不是「帳號切換」）。無 pin
        # 時回 None，照常落到下面的 pause 檢查，暫停行為不變。
        pinned_to = _maybe_apply_pinned_account()
        if pinned_to:
            _write_status(
                "rotate_restart",
                sleep_until=time.time() + _ROTATE_RESTART_SLEEP,
                quota={"pinned_to": pinned_to},
            )
            await asyncio.sleep(_ROTATE_RESTART_SLEEP)
            continue

        if config.autopilot_paused():
            await _pause_tick()
            continue
        _note_resumed()

        # 額度閘門：取任務前先確認至少一個 provider 還有額度；全受限就睡到最早重置
        # （夾在 [60, AUTOPILOT_QUOTA_MAX_SLEEP]）後 continue 重查，避免額度耗盡時
        # 空轉把任務全燒成 failed。snapshot() 含阻塞 I/O，丟 to_thread 跑。
        quota: dict[str, float | None] = {}
        if config.AUTOPILOT_QUOTA_GATE:
            snap = await asyncio.to_thread(provider_quota.snapshot)
            quota = _quota_summary(snap)
            # Claude 訂閱雙帳號負載平衡：必須在 gate 的睡眠判斷「之前」——gate 只看得到
            # 在線帳號的額度，若在線帳號達上限而另一帳號仍有額度，先切換才不會被 gate
            # 誤判「全受限」睡到重置。命中即已排程服務重啟，本輪不取任務，睡短暫等
            # systemd 重啟接手。
            rotated = _maybe_rotate_claude_account(snap)
            if rotated:
                quota = {**quota, "rotated_to": rotated}
                _write_status(
                    "rotate_restart",
                    sleep_until=time.time() + _ROTATE_RESTART_SLEEP,
                    quota=quota,
                )
                await asyncio.sleep(_ROTATE_RESTART_SLEEP)
                continue
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
                # 每個受限期只通知一次（醒來仍受限不重發；恢復 usable 後重置旗標）。
                global _quota_notified
                if not _quota_notified:
                    _quota_notified = True
                    notify.send_bg(
                        "quota_exhausted",
                        f"所有 provider 額度受限，休眠 {sleep_s:.0f}s 等重置",
                        quota={k: v for k, v in quota.items() if v is not None},
                    )
                await asyncio.sleep(sleep_s)
                continue
            _quota_notified = False

        # 每日 PR 成本熔斷：達上限即睡到 UTC 跨日（夾 AUTOPILOT_QUOTA_MAX_SLEEP 上限，
        # 醒來重查），期間連 discovery（_evaluate_self）也不跑——省下註定無法出貨的
        # LLM 成本。dryrun 不打真 PR，不受限。
        if not config.AUTOPILOT_DRYRUN and _daily_pr_budget_exceeded():
            now = time.time()
            sleep_s = min(
                max(_next_utc_midnight(now) - now, 60.0),
                float(config.AUTOPILOT_QUOTA_MAX_SLEEP),
            )
            _write_status("budget_sleep", sleep_until=now + sleep_s, quota=quota)
            log.info(
                "已達每日 PR 預算 %d，休眠 %.0f 秒（UTC 跨日自動恢復）",
                config.AUTOPILOT_DAILY_PR_BUDGET,
                sleep_s,
            )
            await asyncio.sleep(sleep_s)
            continue

        _maybe_triage_failed()
        # Rule 2：Rule 1（triage_failed）退不了的歷史 timeout-parked，每輪挑 1 筆交專家自動拆分。
        await _maybe_triage_timeout_parked()
        _recover_stale_in_progress()
        # 規範迴路(A3,灰度):人工介入/失敗事件 → 蒸餾成慣例入 lessons(每日一次)。
        await _maybe_norms_distill()
        # 任務邊界部署自查：放在取任務之前——此刻保證無 autopilot 討論，是 autodeploy
        # 飢餓下唯一可靠的部署窗口（成功且自身碼有變會 execv，不返回）。
        await _maybe_boundary_redeploy()
        # open PR reconciler：放在取任務之前——「PR 已合併但任務被退回 pending」的場景
        # 要先收斂成 done，堵住重複開工。
        await _maybe_reconcile_open_prs()
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
        global _task_running
        _task_running = True
        try:
            await run_one_task(task)
        except AutopilotTaskStalled as exc:
            # 執行中活動停滯（疑似子程序死鎖）＝基礎設施型失敗，非任務太大：標 failed 且 note
            # 含「逾時」命中 INFRA_FAILURE_RE，下一輪頂端的 _maybe_triage_failed 即把 attempts
            # 歸零、退回 pending 自動重試（無需外部監控/人工重啟）。與硬牆逾時的 parked 區分。
            backlog.set_status(
                task["id"],
                "failed",
                note=f"任務執行中停滯逾時（{exc}）——標 failed，由分診自動重試",
            )
            log.warning("任務 #%s 執行中停滯逾時，標 failed 待分診重試", task.get("id"))
        except TimeoutError:
            # 任務級 timeout ≠ 任務本身壞死：多半是範圍太大跑不完。交 _handle_task_timeout：能自動
            # 拆成更小子任務再排就拆（原任務歸檔 parked），否則維持舊 parked 行為（而非 failed 死路）
            # 讓 backlog 看得見。session 軟性時間預算已讓多數場次在硬砍前優雅收斂，落到這裡是超支的少數。
            await _handle_task_timeout(task)
        except Exception as exc:  # noqa: BLE001 — 單一任務出錯不該弄死整個迴圈
            log.exception("任務 #%s 例外", task.get("id"))
            backlog.set_status(task["id"], "failed", note=f"{type(exc).__name__}: {exc}")

        # 部署後若自身程式碼有更新 → 重載自己,避免跑舊邏輯
        _task_running = False
        _loop_tick()
        if not config.AUTOPILOT_DRYRUN and _self_sig() != startup_sig:
            log.info("偵測到 autopilot 自身程式碼更新,os.execv 重載")
            await _prepare_execv_reload()
            os.execv(sys.executable, [sys.executable, "-m", "studio.autopilot"])

        await asyncio.sleep(config.AUTOPILOT_COOLDOWN)


if __name__ == "__main__":
    asyncio.run(main())
