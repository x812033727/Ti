"""QA 驗證：一併納管的敏感寫入端點門禁（原任務 #2 限本機 → 現改 require_admin）。

範圍：POST /api/settings、POST /api/autopilot/{pause,resume,task}。
政策：門禁啟用時僅靠登入門禁（外網登入後可用）；門禁停用時 fail-safe 退回僅限本機。
聚焦驗收標準：
- AC3：這些寫入路由皆掛 require_admin；讀取類路由維持不變（不掛 loopback/admin）。
- AC5（門禁停用 fail-safe 面）：loopback peer → 放行(非403)、公網 peer → 403、
        受信代理偽造 XFF → 403、來源不可知 → 403。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import backlog, config, settings

# 任務 #2 一併納管的寫入端點
TASK2_WRITES = [
    "/api/settings",
    "/api/autopilot/pause",
    "/api/autopilot/resume",
    "/api/autopilot/task",
]
# 讀取類：維持不變，不得掛 require_loopback
READ_ONLY = [
    "/api/settings",  # GET
    "/api/autopilot",  # GET
    "/api/autopilot/backlog",  # GET
]


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


@pytest.fixture
def stub_side_effects(tmp_path, monkeypatch):
    """把寫入端點的底層副作用導向暫存/stub，loopback 放行測試才不污染真實狀態。"""
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "pause.flag")
    monkeypatch.setattr(settings, "update", lambda body: {})
    monkeypatch.setattr(backlog, "add", lambda *a, **k: {"id": "stub", "title": "t"})
    yield


def _route_dep_funcs(app, path, method):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {getattr(d.dependency, "__name__", None) for d in route.dependencies}
    raise AssertionError(f"找不到路由 {method} {path}")


# --- AC3：四個寫入端點 dependencies 掛 require_admin 複合門禁 --------------
@pytest.mark.parametrize("path", TASK2_WRITES)
def test_task2_write_has_admin_dep(app, path):
    funcs = _route_dep_funcs(app, path, "POST")
    assert "require_admin" in funcs, f"POST {path} 缺 require_admin"


# --- AC3：讀取類路由維持不變（不得掛 require_loopback / require_admin）-----
@pytest.mark.parametrize("path", READ_ONLY)
def test_readonly_routes_have_no_loopback(app, path):
    funcs = _route_dep_funcs(app, path, "GET")
    assert "require_loopback" not in funcs, f"GET {path} 不應掛 require_loopback"
    assert "require_admin" not in funcs, f"GET {path} 不應誤掛管理門禁"
    assert "require_auth" in funcs  # 讀取類仍需登入


def test_readonly_routes_allowed_from_public_when_auth_disabled(app, monkeypatch):
    """門禁停用時，公網來源仍可讀取（證明讀取類未被 loopback 誤擋）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=("203.0.113.5", 40000))
    for path in READ_ONLY:
        assert client.get(path).status_code == 200, f"GET {path} 不應被擋"


# --- AC5：公網 peer → 403 ------------------------------------------------
@pytest.mark.parametrize("path", TASK2_WRITES)
def test_public_peer_blocked_403(app, monkeypatch, path):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用也不得放行
    client = TestClient(app, client=("203.0.113.5", 40000))
    r = client.post(path, json={"title": "x"})
    assert r.status_code == 403
    assert r.json()["detail"] == "僅限本機存取"


# --- AC5：來源不可知 → fail-closed 403 -----------------------------------
@pytest.mark.parametrize("path", TASK2_WRITES)
def test_unknown_peer_fail_closed_403(app, monkeypatch, path):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)  # 預設 client host = "testclient"，無法解析為 IP
    assert client.post(path, json={"title": "x"}).status_code == 403


# --- AC5：受信代理偽造 XFF → 403 -----------------------------------------
@pytest.mark.parametrize("path", TASK2_WRITES)
def test_spoofed_xff_blocked_403(app, monkeypatch, path):
    """loopback peer 受信 → 採信 XFF，真實 client 為公網 → 403（最左塞 127 無效）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    monkeypatch.setenv("TI_TRUSTED_PROXIES", "127.0.0.0/8,::1")
    config.reset_trusted_proxies()
    try:
        client = TestClient(app, client=("127.0.0.1", 12345))
        r = client.post(
            path, json={"title": "x"}, headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.9"}
        )
        assert r.status_code == 403
        assert r.json()["detail"] == "僅限本機存取"
    finally:
        config.reset_trusted_proxies()


# --- AC5：loopback peer → 放行（非403，通過 gate 進入 handler）------------
@pytest.mark.parametrize("path", TASK2_WRITES)
def test_loopback_peer_allowed_non_403(app, stub_side_effects, monkeypatch, path):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用，loopback 直接進 handler
    client = TestClient(app, client=("127.0.0.1", 12345))
    r = client.post(path, json={"title": "x"})
    assert r.status_code != 403, f"{path} loopback 來源不應被 403 擋下"
    assert r.status_code == 200
