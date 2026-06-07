"""密碼門禁測試：門禁停用時向後相容、啟用時保護 HTTP / WebSocket 端點。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from studio import auth, config


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


def test_change_password_when_disabled_enables_gate(app, pw_env, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)
    # 門禁停用時可直接設定新密碼以首次啟用門禁（無需目前密碼）
    r = client.post("/api/auth/password", json={"new_password": "newpass"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert config.auth_enabled() is True
    # 新密碼可登入；附帶的 cookie 也讓操作者保持登入
    assert client.post("/api/login", json={"password": "newpass"}).status_code == 200
    assert config.AUTH_COOKIE in client.cookies


def test_change_password_when_enabled(app, pw_env, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "oldpass")
    client = TestClient(app)
    body = {"current_password": "oldpass", "new_password": "brandnew"}

    # 未登入 → 401
    assert client.post("/api/auth/password", json=body).status_code == 401

    client.post("/api/login", json={"password": "oldpass"})
    # 目前密碼錯誤 → 403
    assert (
        client.post(
            "/api/auth/password",
            json={"current_password": "wrong", "new_password": "brandnew"},
        ).status_code
        == 403
    )
    # 新密碼太短 → 400
    assert (
        client.post(
            "/api/auth/password",
            json={"current_password": "oldpass", "new_password": "x"},
        ).status_code
        == 400
    )
    # 正確 → 200，新密碼即時生效
    assert client.post("/api/auth/password", json=body).status_code == 200
    assert auth.check_password("brandnew") is True
    assert auth.check_password("oldpass") is False


def test_token_roundtrip_and_tamper(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    token = auth.make_token()
    assert auth.verify_token(token) is True
    assert auth.verify_token(token + "x") is False
    assert auth.verify_token("garbage") is False
    assert auth.verify_token(None) is False
