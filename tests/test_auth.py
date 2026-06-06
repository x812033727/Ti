"""密碼門禁測試：門禁停用時向後相容、啟用時保護 HTTP / WebSocket 端點。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


# --- 門禁停用（預設）：一切照舊放行 ------------------------------------
def test_auth_disabled_allows_protected_api(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)
    # 首頁回工作室、受保護 API 可存取
    assert client.get("/").status_code == 200
    assert client.get("/api/history").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"auth_enabled": False, "authed": True}


# --- 門禁啟用：未登入被擋、登入後放行 ---------------------------------
def test_auth_enabled_blocks_then_allows(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app)

    # 未登入：受保護 API 回 401
    assert client.get("/api/history").status_code == 401
    assert client.get("/api/auth/status").json() == {"auth_enabled": True, "authed": False}

    # 密碼錯誤：401，不發 cookie
    bad = client.post("/api/login", json={"password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["ok"] is False

    # 密碼正確：200 並下發 cookie；之後受保護 API 放行
    ok = client.post("/api/login", json={"password": "secret"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert config.AUTH_COOKIE in client.cookies
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/auth/status").json()["authed"] is True

    # 登出後再次被擋
    client.post("/api/logout")
    assert client.get("/api/history").status_code == 401


def test_auth_enabled_websocket_requires_login(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app)

    # 未登入：WS 連線會收到 error 並被關閉
    with client.websocket_connect("/ws") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert "登入" in ev["payload"]["message"]


def test_token_roundtrip_and_tamper(monkeypatch):
    from studio import auth

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    token = auth.make_token()
    assert auth.verify_token(token) is True
    assert auth.verify_token(token + "x") is False
    assert auth.verify_token("garbage") is False
    assert auth.verify_token(None) is False
