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
    # 須先通過 loopback 檢查（在身分檢查之前），故用 loopback client 才測得到登入分支
    client = TestClient(app, client=("127.0.0.1", 12345))

    # 未登入：WS 連線會收到 error 並被關閉
    with client.websocket_connect("/ws") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert "登入" in ev["payload"]["message"]


# --- 任務 #4：/ws 於 handler 內限定本機（來源前置於身分） ----------------
def test_ws_blocks_public_peer(app, monkeypatch):
    """公網來源連 /ws：握手後收到『僅限本機存取』error 並被關閉，獨立於門禁狀態。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用也不得放行
    client = TestClient(app, client=("203.0.113.5", 40000))
    with client.websocket_connect("/ws") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["payload"]["message"] == "僅限本機存取"


def test_ws_blocks_unknown_peer(app, monkeypatch):
    """來源不可知（TestClient 預設 host 非 IP）→ fail-closed，/ws 被擋。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)  # 預設 client host = "testclient"
    with client.websocket_connect("/ws") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["payload"]["message"] == "僅限本機存取"


def test_ws_loopback_check_precedes_auth(app, monkeypatch):
    """來源前置於身分：門禁啟用且公網來源時，先因 loopback 被擋（非『需要登入』）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app, client=("203.0.113.5", 40000))
    with client.websocket_connect("/ws") as ws:
        ev = ws.receive_json()
        assert ev["payload"]["message"] == "僅限本機存取"  # loopback 先於登入短路


def test_ws_allows_loopback_peer(app, monkeypatch):
    """loopback 來源放行 loopback 檢查：送空需求應進入 handler 主體並回『需求不可為空』。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=("127.0.0.1", 12345))
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": ""})  # 已過 loopback+auth，進入需求解析
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["payload"]["message"] == "需求不可為空"


def test_change_password_when_disabled_enables_gate(app, pw_env, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    # password 端點限定本機，TestClient 預設 host 非 IP（fail-closed），需指定 loopback client
    client = TestClient(app, client=("127.0.0.1", 12345))
    # 門禁停用時可直接設定新密碼以首次啟用門禁（無需目前密碼）
    r = client.post("/api/auth/password", json={"new_password": "newpass"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert config.auth_enabled() is True
    # 新密碼可登入；附帶的 cookie 也讓操作者保持登入
    assert client.post("/api/login", json={"password": "newpass"}).status_code == 200
    assert config.AUTH_COOKIE in client.cookies


def test_change_password_when_enabled(app, pw_env, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "oldpass")
    # password 端點限定本機，需指定 loopback client 才能測到 401/403/400 等下游邏輯
    client = TestClient(app, client=("127.0.0.1", 12345))
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


# --- 任務 #1/#2：敏感寫入端點限定本機（require_loopback） ----------------
# 守門清單：列舉所有 WRITE_DEPS 端點，任一漏掛或未來新增未掛皆會被本測試攔下。
LOOPBACK_WRITE_ENDPOINTS = [
    "/api/redeploy",
    "/api/auth/password",
    "/api/settings",
    "/api/autopilot/pause",
    "/api/autopilot/resume",
    "/api/autopilot/task",
]


@pytest.mark.parametrize("path", LOOPBACK_WRITE_ENDPOINTS)
def test_high_risk_endpoint_blocks_public_peer(app, monkeypatch, path):
    """公網來源對敏感寫入端點一律 403，且獨立於門禁狀態（loopback 在 auth 之前短路）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用也不得放行
    client = TestClient(app, client=("203.0.113.5", 40000))
    r = client.post(path, json={})
    assert r.status_code == 403
    assert r.json()["detail"] == "僅限本機存取"


@pytest.mark.parametrize("path", LOOPBACK_WRITE_ENDPOINTS)
def test_high_risk_endpoint_blocks_unknown_peer(app, monkeypatch, path):
    """來源不可知（TestClient 預設 host 非 IP）→ fail-closed 回 403。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)  # 預設 client host = "testclient"，無法解析為 IP
    assert client.post(path, json={}).status_code == 403


def test_high_risk_endpoint_allows_loopback_peer(app, pw_env, monkeypatch):
    """loopback 來源放行 require_loopback：以 password 端點驗證（不觸發實際重啟）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=("127.0.0.1", 12345))
    r = client.post("/api/auth/password", json={"new_password": "loopok"})
    assert r.status_code == 200  # 通過 loopback + auth，進入 handler


@pytest.mark.parametrize("path", LOOPBACK_WRITE_ENDPOINTS)
def test_high_risk_endpoint_rejects_spoofed_xff(app, monkeypatch, path):
    """裸 XFF 偽造：trust_proxy 預設關閉時，公網 peer 偽造 X-Forwarded-For: 127.0.0.1 仍 403。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=("203.0.113.5", 40000))
    r = client.post(path, json={}, headers={"X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 403  # XFF 被忽略，採信 socket peer（公網）


def test_read_endpoints_not_loopback_restricted(app, monkeypatch):
    """讀取類 GET 不受 loopback 限定：公網來源於門禁停用時仍可存取。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=("203.0.113.5", 40000))
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/autopilot").status_code == 200
    assert client.get("/api/history").status_code == 200


# --- 任務 #3：讀取類端點不納管本機限定，但仍保有門禁 --------------------
# settings GET、workspace 查詢、history 查詢等讀取面：不掛 require_loopback，
# 維持 require_auth。以結構反查鎖死，防止未來誤把 loopback 掛到讀取端點。
READ_ENDPOINTS = [
    ("GET", "/api/settings"),
    ("GET", "/api/autopilot"),
    ("GET", "/api/autopilot/backlog"),
    ("GET", "/api/history"),
    ("GET", "/api/history/{session_id}/events"),
    ("GET", "/api/workspace/{session_id}/files"),
    ("GET", "/api/workspace/{session_id}/file"),
    ("GET", "/api/workspace/{session_id}/download"),
    ("GET", "/api/publish/config"),
]


def _route_dep_names(app, method, path):
    for r in app.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return [d.call.__name__ for d in r.dependant.dependencies]
    raise AssertionError(f"route not found: {method} {path}")


@pytest.mark.parametrize("method,path", READ_ENDPOINTS)
def test_read_endpoint_keeps_auth_without_loopback(app, method, path):
    """讀取類端點：不含 require_loopback（不納管），但仍含 require_auth（門禁照舊）。"""
    deps = _route_dep_names(app, method, path)
    assert "require_loopback" not in deps, f"{method} {path} 不應被 loopback 限定"
    assert "require_auth" in deps, f"{method} {path} 應維持門禁保護"


def test_read_endpoint_blocked_when_auth_enabled(app, monkeypatch):
    """門禁啟用且未登入時，讀取類仍回 401（證明 require_auth 仍生效，非被 loopback 取代）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app, client=("203.0.113.5", 40000))  # 公網來源也只受 auth 約束
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/history").status_code == 401


def test_token_roundtrip_and_tamper(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    token = auth.make_token()
    assert auth.verify_token(token) is True
    assert auth.verify_token(token + "x") is False
    assert auth.verify_token("garbage") is False
    assert auth.verify_token(None) is False
