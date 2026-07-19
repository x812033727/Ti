"""自我重佈管線：把 merge 進 main 的成果拉進部署目錄、重裝、重啟服務，並做健康檢查；
失敗則自動回滾到上一個好 commit。

這是全自動 merge+重佈能成立的存活底線——沒有它,一個壞 commit 就會把服務（連同
autopilot 的暫停開關）一起弄死。由 autopilot 程序以 root 呼叫。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import shutil
import subprocess
import time

from . import config, notify, runner


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


# 黑盒探針(第 4 階 B1):部署後除 liveness(health_check)外,再驗證幾條真實 API 契約
# ——「服務起來了」≠「服務是對的」。探針刻意極小、零依賴(curl):health JSON 契約、
# auth 握手、前端殼。(path, body 必含子字串;空=只驗 200)。
_BLACKBOX_PROBES: tuple[tuple[str, str], ...] = (
    ("/api/health", '"ok"'),
    ("/api/auth/status", '"auth_enabled"'),
    ("/", ""),
)


async def blackbox_verify(base: str | None = None) -> tuple[bool, str]:
    """部署後 API 契約黑盒驗證;回 (ok, msg)。base 預設由 AUTOPILOT_HEALTH_URL 推導。"""
    base = (base or config.AUTOPILOT_HEALTH_URL).rsplit("/api/", 1)[0].rstrip("/")
    for path, needle in _BLACKBOX_PROBES:
        url = base + path
        rc, out = await _run(
            ["curl", "-sL", "--max-time", "5", "-w", "\n%{http_code}", url], timeout=15
        )
        body, _, code = (out or "").rpartition("\n")
        if rc != 0 or code.strip() != "200" or (needle and needle not in body):
            return False, f"黑盒探針失敗:{path}(HTTP {code.strip() or '?'})"
    return True, f"黑盒探針通過({len(_BLACKBOX_PROBES)} 條契約)"


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

        # deploy_dir 是 origin 單向鏡像；force refspec 避免並行 fetch 的 ref CAS 競爭。
        rc, out = await _run(
            ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=deploy_dir,
            timeout=120,
        )
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
        if ok and config.DEPLOY_VERIFY:
            ok, msg = await blackbox_verify()

        if not ok:
            rb_ok, rb_msg = await rollback(last_good)
            detail = f"重佈失敗（{msg}）→ 回滾{'成功' if rb_ok else '也失敗'}：{rb_msg}"
            # page 級推播(B1):部署失敗+回滾必須到人——過去這條路徑是靜默的,人要開面板
            # 才會發現服務被回滾。推播失敗不影響回滾結果(send_bg 吞掉一切)。
            notify.send_bg("deploy_verify_failed", detail[:300], rollback_ok=rb_ok)
            return False, detail

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


# --- 部署漂移可觀測（完成率第三輪修法二A）----------------------------------

# 模組級 TTL 快取：/api/autopilot 每次 poll 都 fork git 太重；30 秒內共用同一份快照。
_drift_cache: dict = {"ts": 0.0, "data": None}
_DRIFT_TTL_S = 30.0


async def drift_stats() -> dict:
    """部署漂移快照：{"disk_head", "origin_head", "behind", "deferred"}，30s TTL 快取。

    純唯讀、不打網路：rev-parse/rev-list 只讀本地 refs，origin/<branch> 的新鮮度靠
    autodeploy timer 與任務邊界檢查的 fetch 保鮮。behind＝磁碟碼落後 origin 的 commit 數
    （0＝同步；None＝取不到，如 origin ref 尚不存在）。deferred＝autodeploy「有討論延後」
    觀測檔（autodeploy-deferred.json，寫入端可後補；缺檔回 None）。任何 git 失敗回空欄
    位，絕不拋——觀測面不得弄死 API。
    """
    now = time.time()
    if _drift_cache["data"] is not None and now - _drift_cache["ts"] < _DRIFT_TTL_S:
        return _drift_cache["data"]
    deploy_dir = str(config.AUTOPILOT_DEPLOY_DIR)
    branch = config.AUTOPILOT_BRANCH
    disk = origin = ""
    behind = None
    try:
        # 不用 current_head：它不檢查 rc，git 失敗時會把錯誤文字誤當 head。
        rc, out = await _run(["git", "rev-parse", "HEAD"], cwd=deploy_dir, timeout=30)
        if rc == 0:
            disk = out.strip()
        rc, out = await _run(["git", "rev-parse", f"origin/{branch}"], cwd=deploy_dir, timeout=30)
        if rc == 0:
            origin = out.strip()
        if disk and origin:
            rc, cnt = await _run(
                ["git", "rev-list", "--count", f"HEAD..origin/{branch}"],
                cwd=deploy_dir,
                timeout=30,
            )
            if rc == 0 and cnt.strip().isdigit():
                behind = int(cnt.strip())
    except Exception:  # noqa: BLE001 — 觀測面不得弄死呼叫端
        pass
    deferred = None
    try:
        raw = (config.AUTOPILOT_STATE_DIR / "autodeploy-deferred.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            deferred = parsed
    except (OSError, ValueError):
        deferred = None
    data = {
        "disk_head": disk[:12],
        "origin_head": origin[:12],
        "behind": behind,
        "deferred": deferred,
    }
    _drift_cache["ts"] = now
    _drift_cache["data"] = data
    return data
