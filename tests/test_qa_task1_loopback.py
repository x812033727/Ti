"""QA 驗證：高危寫入端點門禁（原任務 #1 限本機 → 現改 require_admin 複合門禁）。

政策變更：管理寫入端點不再無條件限定本機。門禁啟用（設了 TI_ACCESS_PASSWORD）時
僅靠登入門禁（外網登入後可重新部署/改設定）；門禁停用時 fail-safe 退回僅限本機，
不把控制面裸露給全網。require_loopback / netutil.is_loopback 保留為 fail-safe
分支的實作，本檔 AC1 單元測試原樣沿用。

聚焦驗收標準：
- AC1：require_loopback 存在；未通過 raise HTTPException(403)；確實『呼叫』netutil.is_loopback
        （非字串比對 127.0.0.1）。
- AC2：POST /api/redeploy 與 POST /api/auth/password 的 dependencies 掛 require_admin
        （複合門禁，取代直接並掛 require_loopback + require_auth）。
- AC5（門禁停用時的 fail-safe 面）：loopback peer → 放行(非403)、公網 peer → 403、
        受信代理偽造 XFF → 403。
- AC7：不引入第三方套件（僅用標準庫 + 既有相依）。
"""

from __future__ import annotations

import os

import pytest
from _routes import iter_routes
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from studio import auth, config, netutil

HIGH_RISK = ["/api/redeploy", "/api/auth/password"]


def make_request(peer=None, xff=None):
    scope = {"type": "http", "headers": []}
    if peer is not None:
        scope["client"] = (peer, 12345)
    if xff is not None:
        values = [xff] if isinstance(xff, str) else list(xff)
        scope["headers"] = [(b"x-forwarded-for", v.encode()) for v in values]
    return Request(scope)


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


@pytest.fixture
def pw_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    saved = os.environ.get("TI_ACCESS_PASSWORD")
    yield
    if saved is None:
        os.environ.pop("TI_ACCESS_PASSWORD", None)
    else:
        os.environ["TI_ACCESS_PASSWORD"] = saved


# --- AC1：依賴存在、未通過拋 403、且確實委派給 netutil.is_loopback --------
def test_require_loopback_exists_and_callable():
    assert callable(auth.require_loopback)


def test_require_loopback_raises_403_when_not_loopback(monkeypatch):
    monkeypatch.setattr(netutil, "is_loopback", lambda scope: False)
    with pytest.raises(HTTPException) as ei:
        auth.require_loopback(make_request(peer="203.0.113.9"))
    assert ei.value.status_code == 403


def test_require_loopback_passes_when_loopback(monkeypatch):
    monkeypatch.setattr(netutil, "is_loopback", lambda scope: True)
    # 不應拋出任何例外
    assert auth.require_loopback(make_request(peer="127.0.0.1")) is None


def test_require_loopback_actually_calls_netutil_is_loopback(monkeypatch):
    """AC1 關鍵：判定確實委派 netutil.is_loopback，而非自行字串比對 127.0.0.1。"""
    calls = []

    def spy(scope):
        calls.append(scope)
        return True

    monkeypatch.setattr(netutil, "is_loopback", spy)
    auth.require_loopback(make_request(peer="127.0.0.1"))
    assert len(calls) == 1  # 確有呼叫


def test_require_loopback_detail_does_not_leak_source(monkeypatch):
    """403 detail 維持泛化，不得回傳 client_ip／XFF 等來源資訊。"""
    monkeypatch.setattr(netutil, "is_loopback", lambda scope: False)
    with pytest.raises(HTTPException) as ei:
        auth.require_loopback(make_request(peer="203.0.113.9", xff="8.8.8.8"))
    assert ei.value.detail == "僅限本機存取"
    assert "203.0.113.9" not in str(ei.value.detail)
    assert "8.8.8.8" not in str(ei.value.detail)


# --- AC2：兩端點 dependencies 掛 require_admin 複合門禁 --------------------
def _route_dep_funcs(app, path):
    for route in iter_routes(app):
        if getattr(route, "path", None) == path:
            return {getattr(d.dependency, "__name__", None) for d in route.dependencies}
    raise AssertionError(f"找不到路由 {path}")


@pytest.mark.parametrize("path", HIGH_RISK)
def test_high_risk_route_has_admin_dep(app, path):
    funcs = _route_dep_funcs(app, path)
    assert "require_admin" in funcs, f"{path} 缺 require_admin"
    # 複合門禁取代直接並掛：兩個舊依賴不再直接出現在路由上
    assert "require_loopback" not in funcs, f"{path} 不應再直接掛 require_loopback"
    assert "require_auth" not in funcs, f"{path} 不應再直接掛 require_auth"


# --- AC5（門禁停用 fail-safe）：loopback 放行 / 公網 403 / 偽造 XFF 403 ----
@pytest.mark.parametrize("path", HIGH_RISK)
def test_public_peer_blocked_403(app, monkeypatch, path):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → fail-safe 限本機
    client = TestClient(app, client=("203.0.113.5", 40000))
    r = client.post(path, json={})
    assert r.status_code == 403
    assert r.json()["detail"] == "僅限本機存取"


def test_loopback_peer_allowed_non_403(app, pw_env, monkeypatch):
    """以 password 端點驗證 loopback 放行（不觸發實際 redeploy 重啟）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=("127.0.0.1", 12345))
    r = client.post("/api/auth/password", json={"new_password": "loopok"})
    assert r.status_code != 403
    assert r.status_code == 200


@pytest.mark.parametrize("path", HIGH_RISK)
def test_spoofed_xff_blocked_403(app, monkeypatch, path):
    """受信代理（loopback peer）+ 最左偽造 127.0.0.1，真實 client 為公網 → 仍 403。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    monkeypatch.setenv("TI_TRUSTED_PROXIES", "127.0.0.0/8,::1")
    config.reset_trusted_proxies()
    try:
        client = TestClient(app, client=("127.0.0.1", 12345))
        # peer 受信 → 採信 XFF；由右往左取最右非受信 = 203.0.113.9（公網）
        r = client.post(path, json={}, headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.9"})
        assert r.status_code == 403
        assert r.json()["detail"] == "僅限本機存取"
    finally:
        config.reset_trusted_proxies()


def test_unknown_peer_fail_closed_403(app, monkeypatch):
    """來源不可知（TestClient 預設 host 非 IP）→ fail-closed 403。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)  # 預設 client host = "testclient"
    for path in HIGH_RISK:
        assert client.post(path, json={}).status_code == 403


# --- AC7：未引入第三方套件（auth 僅用標準庫 + dotenv/fastapi 既有相依）----
def test_no_new_third_party_import():
    import ast
    from pathlib import Path

    src = Path(auth.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed_top = {
        "base64",
        "hashlib",
        "hmac",
        "os",
        "time",
        "__future__",
        "dotenv",
        "fastapi",  # 既有相依
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                assert n.name.split(".")[0] in allowed_top, f"非預期 import: {n.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:  # 絕對 import
                assert node.module.split(".")[0] in allowed_top, f"非預期 import: {node.module}"
