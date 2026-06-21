"""單一共用密碼門禁：簽發 / 驗證 session cookie，並提供 FastAPI 依賴與 WebSocket 檢查。

設計重點：未設定 TI_ACCESS_PASSWORD 時門禁完全停用（向後相容），所有檢查直接放行。
不引入額外依賴，cookie token 以標準庫 hmac 簽章（含簽發時間戳，逾時失效）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time

from fastapi import HTTPException, Request, WebSocket

from . import config, netutil
from .secretfile import write_secret_file

log = logging.getLogger("ti.auth")


def _sign(payload: bytes) -> str:
    sig = hmac.new(config.AUTH_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def make_token() -> str:
    """產生帶簽發時間戳的簽章 token。"""
    payload = str(int(time.time())).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def verify_token(token: str | None) -> bool:
    """驗證 token 簽章正確且未逾時。"""
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        issued = int(payload.decode())
    except (ValueError, UnicodeDecodeError):
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    return (time.time() - issued) <= config.AUTH_TTL


def check_password(password: str) -> bool:
    """常數時間比較使用者輸入的密碼與設定的密碼。"""
    return hmac.compare_digest(password or "", config.ACCESS_PASSWORD)


def set_password(new_password: str) -> None:
    """設定 / 變更存取密碼：寫入 .env、更新環境變數與 config（即時生效，無需重啟）。

    設為非空字串即啟用門禁；設為空字串則停用門禁。既有的登入 cookie 以 AUTH_SECRET
    簽章、與密碼無關，因此變更密碼不會把目前使用者登出（新登入才需要用新密碼）。
    """
    new_password = (new_password or "").strip()
    write_secret_file(config.env_path(), "TI_ACCESS_PASSWORD", new_password)
    os.environ["TI_ACCESS_PASSWORD"] = new_password
    config.ACCESS_PASSWORD = new_password
    if new_password:
        log.warning("存取密碼已變更，門禁已啟用")
    else:
        log.warning("存取密碼已清空，門禁已停用")


# --- 登入失敗速率限制（防密碼暴力破解）------------------------------------
# 記憶體內的 per-client 連續失敗計數；達上限即鎖定一段時間，期間直接拒絕（不比對密碼）。
# 純記憶體、重啟即清空（自用單機面板足夠；無需引入 redis）。成功登入立刻清除該來源計數。
_LOGIN_FAILS: dict[str, list[float]] = {}  # client -> [連續失敗次數, 鎖定到期 epoch]
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 60.0


def login_lock_remaining(client: str) -> float:
    """此來源目前剩餘鎖定秒數（0＝未鎖定，可嘗試）。"""
    rec = _LOGIN_FAILS.get(client)
    if not rec:
        return 0.0
    remaining = rec[1] - time.time()
    return remaining if remaining > 0 else 0.0


def register_login_result(client: str, ok: bool) -> None:
    """登入結果計入速率限制：成功清除計數；失敗累加，達上限則鎖定 LOGIN_LOCK_SECONDS。"""
    if ok:
        _LOGIN_FAILS.pop(client, None)
        return
    now = time.time()
    rec = _LOGIN_FAILS.get(client)
    fails = int(rec[0]) + 1 if rec else 1
    locked_until = now + LOGIN_LOCK_SECONDS if fails >= LOGIN_MAX_FAILS else 0.0
    _LOGIN_FAILS[client] = [float(fails), locked_until]
    if locked_until:
        log.warning("登入連續失敗 %d 次，來源 %s 鎖定 %ds", fails, client, int(LOGIN_LOCK_SECONDS))


def is_authed(scope: Request | WebSocket) -> bool:
    """門禁停用時恆為 True；啟用時依 cookie 判斷是否已登入。"""
    if not config.auth_enabled():
        return True
    return verify_token(scope.cookies.get(config.AUTH_COOKIE))


def require_auth(request: Request) -> None:
    """FastAPI 依賴：保護 HTTP 路由，未通過回 401。"""
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="需要登入")


def require_loopback(request: Request) -> None:
    """限定本機來源，非本機回 403（現作為 require_admin 門禁停用時的 fail-safe 分支）。

    判定委派給 spoof-safe、fail-closed 的 netutil.is_loopback（禁止字串比對 127.0.0.1）。
    403 detail 維持泛化，不回傳 client_ip／XFF 等內部來源資訊。
    """
    if not netutil.is_loopback(request):
        raise HTTPException(status_code=403, detail="僅限本機存取")


def require_admin(request: Request) -> None:
    """FastAPI 依賴：管理寫入端點門禁（fail-safe 複合依賴）。

    - 門禁啟用（已設 TI_ACCESS_PASSWORD）：等同 require_auth → 已登入的外網使用者
      可操作管理面（重新部署/設定/autopilot），未登入回 401。
    - 門禁停用：退回 require_loopback 僅限本機（403）。is_authed 在門禁停用時恆 True，
      若直接沿用會把控制面（settings 可改 OPENAI_BASE_URL、redeploy、autopilot 注入）
      裸露給所有能連到服務的人（HOST 預設 0.0.0.0），故 fail-safe 收緊為本機。
    """
    if config.auth_enabled():
        require_auth(request)
    else:
        require_loopback(request)
