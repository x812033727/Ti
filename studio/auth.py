"""單一共用密碼門禁：簽發 / 驗證 session cookie，並提供 FastAPI 依賴與 WebSocket 檢查。

設計重點：未設定 TI_ACCESS_PASSWORD 時門禁完全停用（向後相容），所有檢查直接放行。
不引入額外依賴，cookie token 以標準庫 hmac 簽章（含簽發時間戳，逾時失效）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request, WebSocket

from . import config


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


def is_authed(scope: Request | WebSocket) -> bool:
    """門禁停用時恆為 True；啟用時依 cookie 判斷是否已登入。"""
    if not config.auth_enabled():
        return True
    return verify_token(scope.cookies.get(config.AUTH_COOKIE))


def require_auth(request: Request) -> None:
    """FastAPI 依賴：保護 HTTP 路由，未通過回 401。"""
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="需要登入")
