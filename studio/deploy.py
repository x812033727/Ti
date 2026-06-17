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
    """部署後健康檢查：服務回 200，且主機沙箱依賴齊全（避免改動弱化安全）。

    早夭偵測：本設計沿用 run_http_demo（定義於 `runner.run_http_demo`）的「server 進程
    退出即停等」原則，落到 systemctl 服務上——每輪 curl 之前先以
    `systemctl is-active <AUTOPILOT_SERVICE>` 查服務存活，
    服務已被 systemd 標記為 `failed` / `inactive` / `unknown`（或 stdout 空）即提前回
    `(False, …)`，不耗滿 `attempts × delay`（預設 ≈36s）。`active` / `activating` 視為
    服務仍在線、不早退（`activating` 是 systemd 啟動流程中正常狀態，不該被誤判為死）。
    以 stdout 解析狀態而非 returncode，避免兩條早退路徑分叉；查詢本身失敗（rc≠0 且
    stdout 空）也走早退（fail-closed：真值不可得＝視為死）。

    環境探測：入口以 `shutil.which("systemctl")` 偵測，無 systemd 的環境（如容器內、採
    docker compose / supervisord 託管的部署）走 fail-open 略過早夭判定、跑原 attempts
    邏輯——避免把非 systemd 託管的健康服務誤判早退。service 拼錯（`is-active=unknown`）
    仍走早退分流，覆蓋由 `is-active` 本身的回傳負責。

    早退訊息契約：`f"服務啟動後即退出（is-active={state}），未回應 {url}"`——含「退出」
    二字呼應 `run_http_demo` 早退訊息 `服務啟動後即退出（exit=…）`、含 url 助 rollback
    端到端可觀測；早退回 `(False, …)` 在 `redeploy()` / `rollback()` 走既有
    `if not ok: rollback(...)` 同一條路，不繞過、不弱化沙箱依賴檢查。
    """
    url = url or config.AUTOPILOT_HEALTH_URL
    service = config.AUTOPILOT_SERVICE
    use_isactive = shutil.which("systemctl") is not None  # 無 systemd → fail-open
    for _ in range(attempts):
        if use_isactive:
            _rc, isactive_out = await _run(["systemctl", "is-active", service], timeout=5)
            state = isactive_out.strip()
            if state not in ("active", "activating"):
                # failed / inactive / unknown / stdout 空（查詢失敗）→ 早退
                return False, (f"服務啟動後即退出（is-active={state or 'unknown'}），未回應 {url}")
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
