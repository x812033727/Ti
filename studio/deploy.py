"""自我重佈管線：把 merge 進 main 的成果拉進部署目錄、重裝、重啟服務，並做健康檢查；
失敗則自動回滾到上一個好 commit。

這是全自動 merge+重佈能成立的存活底線——沒有它,一個壞 commit 就會把服務（連同
autopilot 的暫停開關）一起弄死。由 autopilot 程序以 root 呼叫。
"""

from __future__ import annotations

import asyncio

from . import config


async def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600) -> tuple[int, str]:
    """執行一個指令（exec，不經 shell），回傳 (returncode, 合併輸出)。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with __import__("contextlib").suppress(ProcessLookupError):
            proc.kill()
        return -1, f"(逾時 {timeout}s)"
    return proc.returncode if proc.returncode is not None else -1, out.decode("utf-8", "replace")


async def current_head(repo_dir: str) -> str:
    _rc, out = await _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, timeout=30)
    return out.strip()


async def health_check(url: str | None = None, attempts: int = 12, delay: int = 3) -> tuple[bool, str]:
    """部署後健康檢查：服務回 200，且主機沙箱依賴齊全（避免改動弱化安全）。"""
    url = url or config.AUTOPILOT_HEALTH_URL
    for _ in range(attempts):
        rc, out = await _run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", url],
            timeout=10,
        )
        if out.strip() == "200":
            deps = config.sandbox_missing_deps()
            if deps:
                return False, f"服務 200 但沙箱依賴缺失：{deps}（拒絕視為健康）"
            return True, "健康檢查通過（200 + 沙箱依賴齊全）"
        await asyncio.sleep(delay)
    return False, f"健康檢查失敗：{url} 在 {attempts} 次內未回 200"


async def _reinstall_and_restart(deploy_dir: str, service: str) -> tuple[bool, str]:
    pip = str(config.AUTOPILOT_DEPLOY_DIR / ".venv" / "bin" / "pip")
    rc, out = await _run([pip, "install", "-e", "."], cwd=deploy_dir, timeout=600)
    if rc != 0:
        return False, f"pip install 失敗：\n{out[-800:]}"
    rc, out = await _run(["systemctl", "restart", service], timeout=120)
    if rc != 0:
        return False, f"systemctl restart 失敗：\n{out[-400:]}"
    return True, "已重裝並重啟"


async def redeploy() -> tuple[bool, str]:
    """把部署目錄拉到 origin/<branch>、重裝、重啟、健康檢查；失敗自動回滾。

    回傳 (ok, 訊息)。dryrun 模式只回報不實作。
    """
    deploy_dir = str(config.AUTOPILOT_DEPLOY_DIR)
    service = config.AUTOPILOT_SERVICE
    branch = config.AUTOPILOT_BRANCH

    if config.AUTOPILOT_DRYRUN:
        return True, f"[dryrun] 會把 {deploy_dir} 重佈到 origin/{branch} 並重啟 {service}"

    last_good = await current_head(deploy_dir)

    rc, out = await _run(["git", "fetch", "origin", branch], cwd=deploy_dir, timeout=120)
    if rc != 0:
        return False, f"git fetch 失敗：\n{out[-400:]}"
    rc, out = await _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=deploy_dir, timeout=60)
    if rc != 0:
        return False, f"git reset 失敗：\n{out[-400:]}"
    new_head = await current_head(deploy_dir)

    ok, msg = await _reinstall_and_restart(deploy_dir, service)
    if ok:
        ok, msg = await health_check()

    if not ok:
        rb_ok, rb_msg = await rollback(last_good)
        return False, f"重佈失敗（{msg}）→ 回滾{'成功' if rb_ok else '也失敗'}：{rb_msg}"

    return True, f"重佈成功：{last_good[:8]} → {new_head[:8]}"


async def rollback(last_good: str) -> tuple[bool, str]:
    """把部署目錄硬重置回 last_good、重裝、重啟、再健康檢查。"""
    deploy_dir = str(config.AUTOPILOT_DEPLOY_DIR)
    service = config.AUTOPILOT_SERVICE
    if config.AUTOPILOT_DRYRUN:
        return True, f"[dryrun] 會回滾 {deploy_dir} 到 {last_good[:8]}"
    rc, out = await _run(["git", "reset", "--hard", last_good], cwd=deploy_dir, timeout=60)
    if rc != 0:
        return False, f"回滾 git reset 失敗：\n{out[-400:]}"
    ok, msg = await _reinstall_and_restart(deploy_dir, service)
    if not ok:
        return False, msg
    ok, msg = await health_check()
    return ok, (f"回滾到 {last_good[:8]} 後 {msg}")
