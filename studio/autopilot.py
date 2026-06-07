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
import logging
import os
import sys
import time
import uuid
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
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
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
    # timeout/sandbox 顯式帶齊（run_command_exec 預設 sandbox=None 會走 fail-closed）。
    result = await runner.run_command_exec(
        clone, ["python", "-m", "pytest", "-q"], timeout=600, sandbox=True, label="pytest gate"
    )
    return result.ok, result.output[-1500:]


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
    """重佈會 restart ti.service，先等手動討論結束（history 無其他 running session）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        running = [m for m in history.list_sessions() if m.get("status") == "running"]
        if not running:
            return True
        log.info("有進行中的討論，延後重佈…(%d)", len(running))
        await asyncio.sleep(15)
    return False


# --- 自我評估 ------------------------------------------------------------


async def _evaluate_self(clone: str) -> int:
    """backlog 空時，用一位資深專家審視 Ti 自身並產出改善任務。回傳新增數。"""
    from .experts import Expert
    from .roles import SENIOR

    sid = f"ap-eval-{uuid.uuid4().hex[:8]}"
    ex = Expert(SENIOR, sid, Path(clone))

    async def _noop(_ev):
        return None

    prompt = (
        "你正在審視「Ti Studio」這個 AI 多專家自主開發工作室專案本身（原始碼就在你的工作目錄）。\n"
        "請用 Read/Grep 快速瀏覽程式碼與測試，找出最值得改善的 3~5 點（bug、缺測試、可讀性、"
        "功能缺口、安全），每點獨立一行,格式固定為 `任務: <動詞開頭的具體任務>`。只輸出任務行。"
    )
    try:
        text = await ex.speak(prompt, _noop)
    finally:
        with contextlib.suppress(Exception):
            await ex.stop()
    tasks = parse_tasks(text)
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
    )
    result = await session.run(requirement)
    history.finish_session(sid)

    # 回饋：討論發現的後續任務寫回 backlog
    if result.get("followups"):
        added = backlog.add_many(result["followups"], source="discovered")
        log.info("從討論新增 %d 個後續任務", added)

    if not result.get("completed"):
        backlog.set_status(task["id"], "failed", note="討論未達完成")
        log.info("任務 #%s 未完成,標 failed", task["id"])
        return

    # 閘門：完整測試必須全綠
    ok, out = await _gate_tests(clone)
    if not ok:
        backlog.set_status(task["id"], "failed", note="測試未通過")
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

    backlog.set_status(task["id"], "done")
    log.info("任務 #%s 完成", task["id"])


def _pause(reason: str) -> None:
    with contextlib.suppress(OSError):
        config.AUTOPILOT_PAUSE_FILE.write_text(f"{reason}\n{time.ctime()}\n", encoding="utf-8")
    log.warning("已暫停 autopilot：%s", reason)


# --- 主迴圈 --------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("autopilot 啟動（dryrun=%s, repo=%s）", config.AUTOPILOT_DRYRUN, config.AUTOPILOT_REPO)
    startup_sig = _self_sig()

    while True:
        if config.autopilot_paused():
            await asyncio.sleep(10)
            continue

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
