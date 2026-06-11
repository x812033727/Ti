"""QA 驗證：管理寫入端點的 require_admin 複合門禁（fail-safe）。

政策：管理寫入端點（redeploy / settings / auth/password / autopilot 三式）不再
無條件限定本機——
- 門禁啟用（設了 TI_ACCESS_PASSWORD）：等同 require_auth，外網**已登入**即可操作
  （重新部署、改設定），未登入回 401「需要登入」。
- 門禁停用：fail-safe 退回 require_loopback 僅限本機（403），不把控制面
  （settings 可改 OPENAI_BASE_URL、redeploy、autopilot 注入）裸露給全網。

核心驗收對應使用者場景：對外網站登入後，「♻️ 重新部署」按鈕可用（不再 403）。
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from studio import auth, backlog, config, netutil, redeploy, settings

ADMIN_WRITES = [
    "/api/redeploy",
    "/api/auth/password",
    "/api/settings",
    "/api/autopilot/pause",
    "/api/autopilot/resume",
    "/api/autopilot/task",
]

PUBLIC_PEER = ("203.0.113.5", 40000)
LOOPBACK_PEER = ("127.0.0.1", 12345)


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


@pytest.fixture
def stub_side_effects(tmp_path, monkeypatch):
    """把各端點底層副作用導向暫存/stub，放行測試才不污染真實狀態（不真的 pull/重啟/寫 .env）。"""
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "pause.flag")
    monkeypatch.setattr(settings, "update", lambda body: {})
    monkeypatch.setattr(backlog, "add", lambda *a, **k: {"id": "stub", "title": "t"})

    async def fake_redeploy(*, restart: bool = True):
        return {"ok": True, "pulled": True, "restarting": False, "detail": "stub"}

    monkeypatch.setattr(redeploy, "redeploy", fake_redeploy)
    yield


def _authed_headers() -> dict:
    """組一個合法登入 cookie 的 header（門禁啟用時用）。"""
    return {"Cookie": f"{config.AUTH_COOKIE}={auth.make_token()}"}


def _payload(path: str) -> dict:
    if path == "/api/auth/password":
        return {"current_password": "secret", "new_password": "brandnew"}
    return {"title": "x"}


# --- require_admin 單元測試：兩分支委派正確 -------------------------------
def _make_request(peer="203.0.113.9"):
    from fastapi import Request

    return Request({"type": "http", "headers": [], "client": (peer, 12345)})


def test_require_admin_delegates_to_auth_when_enabled(monkeypatch):
    """門禁啟用 → 走 require_auth：無 cookie 拋 401（即使來源是公網也不再 403）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    with pytest.raises(HTTPException) as ei:
        auth.require_admin(_make_request())
    assert ei.value.status_code == 401
    assert ei.value.detail == "需要登入"


def test_require_admin_delegates_to_loopback_when_disabled(monkeypatch):
    """門禁停用 → fail-safe 走 require_loopback：非本機拋 403。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(netutil, "is_loopback", lambda scope: False)
    with pytest.raises(HTTPException) as ei:
        auth.require_admin(_make_request())
    assert ei.value.status_code == 403
    assert ei.value.detail == "僅限本機存取"


# --- 核心驗收：公網 + 門禁啟用 + 已登入 → 放行 -----------------------------
@pytest.mark.parametrize("path", ADMIN_WRITES)
def test_public_peer_authed_allowed(app, pw_env, stub_side_effects, monkeypatch, path):
    """對外網站登入後可重新部署/改設定：不再被 401/403 門禁擋下。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app, client=PUBLIC_PEER)
    r = client.post(path, json=_payload(path), headers=_authed_headers())
    assert r.status_code not in (401, 403), f"{path} 已登入外網來源不應被門禁擋下：{r.status_code}"
    assert r.status_code == 200


# --- 公網 + 門禁啟用 + 未登入 → 401 ---------------------------------------
@pytest.mark.parametrize("path", ADMIN_WRITES)
def test_public_peer_unauthed_401(app, monkeypatch, path):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app, client=PUBLIC_PEER)
    r = client.post(path, json=_payload(path))
    assert r.status_code == 401
    assert r.json()["detail"] == "需要登入"


# --- 門禁停用 fail-safe：公網 → 403、loopback → 放行 -----------------------
@pytest.mark.parametrize("path", ADMIN_WRITES)
def test_auth_disabled_public_peer_403(app, monkeypatch, path):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=PUBLIC_PEER)
    r = client.post(path, json=_payload(path))
    assert r.status_code == 403
    assert r.json()["detail"] == "僅限本機存取"


@pytest.mark.parametrize("path", ADMIN_WRITES)
def test_auth_disabled_loopback_allowed(app, pw_env, stub_side_effects, monkeypatch, path):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=LOOPBACK_PEER)
    body = {"new_password": "brandnew"} if path == "/api/auth/password" else _payload(path)
    r = client.post(path, json=body)
    assert r.status_code == 200, f"{path} 門禁停用 + loopback 應放行：{r.status_code}"
