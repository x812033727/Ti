"""首次設定（首次啟用門禁）端到端測試。

涵蓋 in-app 登入態的完整生命週期：門禁停用起始 → `POST /api/auth/password`
不帶目前密碼首次啟用 → `.env` 寫入 → 未登入 401 → 登入取 cookie → 受保護
API / `GET /api/settings` / WS 放行 → 登出後再 401，並補齊 cookie 安全旗標與
負向案例。與 `test_auth.py` 平行，純測試新增、不動產品碼。

防污染唯一邊界是 `pw_env`（沿用已驗證模式）：`.env` 導向 `tmp_path`、還原
`TI_ACCESS_PASSWORD` env；門禁狀態一律以 `monkeypatch.setattr(config, ...)`
切換，禁改 env。所有寫入端點與 WS 一律用 loopback client，避免被
`require_loopback` 先擋成 403。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from studio import config

# 寫入端點 / WS 需 loopback 來源；TestClient 預設 host 非 IP（fail-closed）。
PORT = 12345
LOOPBACK = ("127.0.0.1", PORT)


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


@pytest.fixture
def pw_env(tmp_path, monkeypatch):
    """把 .env 導向暫存目錄，並還原被 set_password 直接改動的環境變數。"""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    saved = os.environ.get("TI_ACCESS_PASSWORD")
    yield
    if saved is None:
        os.environ.pop("TI_ACCESS_PASSWORD", None)
    else:
        os.environ["TI_ACCESS_PASSWORD"] = saved


def _loopback_client(app) -> TestClient:
    """寫入端點 / WS 一律用 loopback client，否則 require_loopback 先短路成 403。"""
    return TestClient(app, client=LOOPBACK)


# --- 主線：首次設定完整生命週期 ----------------------------------------
def test_first_setup_full_lifecycle(app, pw_env, monkeypatch):
    """停用 → 首次設密碼啟用 → .env 寫入 → 401 → 登入 → 放行 → 登出 → 401。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用起始

    # ① 起始：門禁停用、視為已授權
    setup = _loopback_client(app)
    assert setup.get("/api/auth/status").json() == {"auth_enabled": False, "authed": True}

    # ② 首次設定：不帶目前密碼即可設新密碼以啟用門禁
    r = setup.post("/api/auth/password", json={"new_password": "newpass"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["auth_enabled"] is True

    # ③ 門禁已啟用，且 .env 確實寫入 TI_ACCESS_PASSWORD（路徑由 pw_env 控制）
    assert config.auth_enabled() is True
    with open(config.env_path(), encoding="utf-8") as f:
        env_text = f.read()
    assert "TI_ACCESS_PASSWORD" in env_text

    # ④ 乾淨新 client（未持啟用回應 cookie）：未登入受保護 API 回 401
    fresh = _loopback_client(app)
    assert fresh.get("/api/history").status_code == 401
    assert fresh.get("/api/auth/status").json() == {"auth_enabled": True, "authed": False}

    # ⑤ 用新密碼登入，取得 cookie
    login = fresh.post("/api/login", json={"password": "newpass"})
    assert login.status_code == 200 and login.json()["ok"] is True
    assert config.AUTH_COOKIE in fresh.cookies

    # ⑥ 受保護 API / GET /api/settings / WS 皆放行
    assert fresh.get("/api/history").status_code == 200
    assert fresh.get("/api/settings").status_code == 200
    assert fresh.get("/api/auth/status").json()["authed"] is True
    # WS：loopback + 已登入 → 過 loopback+auth 進入 handler（送空需求驗證放行）
    with fresh.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": ""})
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["payload"]["message"] == "需求不可為空"

    # ⑦ 登出沿用同一已登入 client，驗證 delete_cookie 生效 → 再次 401
    fresh.post("/api/logout")
    assert fresh.get("/api/history").status_code == 401


# --- cookie 安全旗標 ---------------------------------------------------
def test_login_cookie_security_flags(app, pw_env, monkeypatch):
    """登入回應的 set-cookie raw header 應含 HttpOnly / SameSite=lax / Path=/。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = _loopback_client(app)

    resp = client.post("/api/login", json={"password": "secret"})
    assert resp.status_code == 200
    # 讀 raw header（旗標資訊只在原始字串完整）；normalize 大小寫避免版本脆裂
    raw = resp.headers["set-cookie"].lower()
    assert config.AUTH_COOKIE.lower() in raw
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "path=/" in raw


def test_first_setup_cookie_security_flags(app, pw_env, monkeypatch):
    """首次啟用回應同樣附帶安全旗標齊備的 cookie，避免操作者當下被登出。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = _loopback_client(app)

    resp = client.post("/api/auth/password", json={"new_password": "newpass"})
    assert resp.status_code == 200
    raw = resp.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "path=/" in raw


# --- 負向案例（依短路順序）--------------------------------------------
def test_enabled_missing_current_password_returns_401(app, pw_env, monkeypatch):
    """門禁已啟用、未登入缺目前密碼：require_auth 先擋成 401（須 loopback client）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = _loopback_client(app)  # loopback 才能越過 require_loopback 走到 require_auth
    r = client.post("/api/auth/password", json={"new_password": "brandnew"})
    assert r.status_code == 401


def test_enabled_wrong_current_password_returns_403(app, pw_env, monkeypatch):
    """已登入但目前密碼錯：403 且 detail 為『目前密碼錯誤』。

    detail 比對用以與 require_loopback 的泛化 403 區分，防假綠燈。
    """
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = _loopback_client(app)
    client.post("/api/login", json={"password": "secret"})  # 先登入越過 require_auth
    r = client.post(
        "/api/auth/password",
        json={"current_password": "wrong", "new_password": "brandnew"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "目前密碼錯誤"


def test_new_password_too_short_returns_400(app, pw_env, monkeypatch):
    """新密碼 <4 字元：400（門禁停用時跳過 current 檢查，直接驗長度）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = _loopback_client(app)
    r = client.post("/api/auth/password", json={"new_password": "x"})
    assert r.status_code == 400


def test_login_wrong_password_issues_no_cookie(app, pw_env, monkeypatch):
    """登入密碼錯：401 且回應不下發 set-cookie。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = _loopback_client(app)
    r = client.post("/api/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert r.json()["ok"] is False
    assert "set-cookie" not in r.headers
    assert config.AUTH_COOKIE not in client.cookies
