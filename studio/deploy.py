"""自我重佈管線：把 merge 進 main 的成果拉進部署目錄、重裝、重啟服務，並做健康檢查；
失敗則自動回滾到上一個好 commit。

這是全自動 merge+重佈能成立的存活底線——沒有它,一個壞 commit 就會把服務（連同
autopilot 的暫停開關）一起弄死。由 autopilot 程序以 root 呼叫。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import shutil
import subprocess

from . import config, runner


def _lock_path():
    return config.AUTOPILOT_STATE_DIR / "deploy.lock"


@contextlib.contextmanager
def _deploy_lock():
    """跨程序序列化部署：autopilot、autodeploy timer、/api/redeploy 共用同一把 flock。

    非阻塞（LOCK_NB）：取不到鎖即代表已有部署進行中 → yield False，呼叫端應略過本輪
    （下一輪 timer/任務會自然補上）。取到鎖 → yield True，離開時釋放。
    """
    config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path().open("w")
    acquired = False
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


async def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600) -> tuple[int, str]:
    """執行一個指令（exec，不經 shell），回傳 (returncode, 合併輸出)。"""
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


async def current_head(repo_dir: str) -> str:
    _rc, out = await _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, timeout=30)
    return out.strip()


async def health_check(
    url: str | None = None, attempts: int = 12, delay: int = 3
) -> tuple[bool, str]:
    """部署後健康檢查：沿用 run_http_demo 的早夭偵測，先看 systemctl is-active。

    有 systemd 時以 is-active 的 failed/inactive/unknown 早退；activating 仍繼續輪詢 HTTP。
    服務回 200 後再確認主機沙箱依賴齊全（避免改動弱化安全）。
    """
    url = url or config.AUTOPILOT_HEALTH_URL
    has_systemctl = shutil.which("systemctl") is not None
    for _ in range(attempts):
        if has_systemctl:
            rc, state_out = await _run(
                ["systemctl", "is-active", config.AUTOPILOT_SERVICE], timeout=10
            )
            state = (state_out.strip().splitlines() or ["unknown"])[0] or "unknown"
            if state in {"failed", "inactive", "unknown"} or (rc != 0 and state == "unknown"):
                return False, f"服務已退出（is-active={state}），停止等待 HTTP 健康檢查"
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


def schedule_service_restart() -> None:
    """1 秒後重啟 ti.service + ti-autopilot，讓換檔後的新 Claude 認證生效。

    Claude 帳號切換（換憑證檔）的共用重啟機制：UI 手動切換端點（routes.py）與 autopilot
    自動輪替共用此函式（SSOT）。認證在 SDK 啟動時載入記憶體，換檔後須重啟兩個服務才生效。
    用 systemd-run 起一次性 transient timer：它脫離呼叫端服務的 cgroup，故 restart 殺掉
    呼叫端（ti.service 或 ti-autopilot 自己）時不會把「重啟動作本身」一起殺掉；1 秒延遲
    確保切換端點的 200 回應已送達前端／autopilot 已寫完心跳。
    無 systemd-run（權限/環境）時退回 detached subprocess，盡力而為。
    """
    cmd = ["systemctl", "restart", "ti.service", "ti-autopilot"]
    try:
        subprocess.Popen(
            ["systemd-run", "--no-block", "--on-active=1", "--", *cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    except OSError:
        pass
    subprocess.Popen(
        ["bash", "-c", "sleep 1; " + " ".join(cmd)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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

    # 跨程序互斥：避免 autopilot / autodeploy timer / /api/redeploy 同時 reset+pip+restart 互撞。
    with _deploy_lock() as acquired:
        if not acquired:
            return False, "另一個部署進行中，略過本輪"

        last_good = await current_head(deploy_dir)

        rc, out = await _run(["git", "fetch", "origin", branch], cwd=deploy_dir, timeout=120)
        if rc != 0:
            return False, f"git fetch 失敗：\n{out[-400:]}"
        rc, out = await _run(
            ["git", "reset", "--hard", f"origin/{branch}"], cwd=deploy_dir, timeout=60
        )
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
