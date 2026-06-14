"""QA 獨立驗證：任務 #3 讀取類路由不納管本機限定（維持不變）。

範圍：settings GET、workspace 查詢、history 查詢等讀取類路由。
驗收要點（AC3 後半）：
- 讀取類路由『不得』掛 require_loopback（避免把合法遠端讀取誤擋）。
- 讀取類路由仍掛 require_auth（門禁啟用時未登入 → 401，行為維持不變）。
- 門禁停用時，公網來源讀取『不會』被 403 擋（證明未受 loopback 影響）。
"""

from __future__ import annotations

import pytest
from _routes import iter_routes
from fastapi.testclient import TestClient

from studio import config

# 讀取類路由（GET 查詢）：(path, method)
READ_ROUTES = [
    ("/api/settings", "GET"),
    ("/api/workspace/{session_id}/files", "GET"),
    ("/api/workspace/{session_id}/file", "GET"),
    ("/api/workspace/{session_id}/download", "GET"),
    ("/api/history", "GET"),
    ("/api/history/{session_id}/events", "GET"),
    ("/api/publish/config", "GET"),
    ("/api/autopilot", "GET"),
    ("/api/autopilot/backlog", "GET"),
]

# 實際可請求的 URL（帶具體 session_id / query），用來打公網/門禁行為
SID = "deadbeefcafe"
READ_URLS = [
    "/api/settings",
    f"/api/workspace/{SID}/files",
    f"/api/workspace/{SID}/file?path=x.txt",
    f"/api/workspace/{SID}/download",
    "/api/history",
    f"/api/history/{SID}/events",
    "/api/publish/config",
    "/api/autopilot",
    "/api/autopilot/backlog",
]


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


def _route_dep_funcs(app, path, method):
    for route in iter_routes(app):
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {getattr(d.dependency, "__name__", None) for d in route.dependencies}
    raise AssertionError(f"找不到路由 {method} {path}")


# --- AC3 後半：讀取類『不掛』require_loopback、但『仍掛』require_auth -------
@pytest.mark.parametrize("path,method", READ_ROUTES)
def test_readonly_not_loopback_restricted(app, path, method):
    funcs = _route_dep_funcs(app, path, method)
    assert "require_loopback" not in funcs, f"{method} {path} 不應掛 require_loopback"
    assert "require_admin" not in funcs, f"{method} {path} 不應誤掛管理門禁"
    assert "require_auth" in funcs, f"{method} {path} 應維持 require_auth"


# --- 門禁停用 + 公網來源 → 不被 403 擋（證明未受 loopback 影響）-----------
@pytest.mark.parametrize("url", READ_URLS)
def test_readonly_public_peer_not_403_when_auth_disabled(app, monkeypatch, url):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用
    client = TestClient(app, client=("203.0.113.5", 40000))  # 公網來源
    r = client.get(url)
    # 核心：不得因 loopback 限定回 403（不存在資源可為 404，但絕非 403）
    assert r.status_code != 403, f"GET {url} 被誤擋 403：{r.status_code}"
    assert r.status_code < 500, f"GET {url} 伺服器錯誤：{r.status_code}"


# --- 門禁啟用 + 未登入 → 401（require_auth 行為維持不變）------------------
@pytest.mark.parametrize("url", READ_URLS)
def test_readonly_requires_auth_when_enabled(app, monkeypatch, url):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")  # 門禁啟用
    client = TestClient(app, client=("203.0.113.5", 40000))  # 即使公網來源
    r = client.get(url)
    # 讀取類靠 require_auth 擋 → 401（而非 loopback 的 403）
    assert r.status_code == 401, f"GET {url} 應回 401，得到 {r.status_code}"
