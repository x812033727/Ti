"""登入失敗限流應使用與來源政策一致的 client IP。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import auth, config


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


@pytest.fixture(autouse=True)
def login_ratelimit_env(monkeypatch):
    auth._LOGIN_FAILS.clear()
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    monkeypatch.setattr(config, "TRUST_PROXY", False)
    monkeypatch.delenv("TI_TRUSTED_PROXIES", raising=False)
    config.reset_trusted_proxies()
    yield
    auth._LOGIN_FAILS.clear()
    monkeypatch.delenv("TI_TRUSTED_PROXIES", raising=False)
    config.reset_trusted_proxies()


def _fail_login(client: TestClient, times: int = auth.LOGIN_MAX_FAILS) -> None:
    for _ in range(times):
        resp = client.post("/api/login", json={"password": "wrong"})
        assert resp.status_code in {401, 429}


def test_login_ratelimit_uses_forwarded_client_ip_for_trusted_proxy(app, monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    monkeypatch.setenv("TI_TRUSTED_PROXIES", "10.0.0.0/8")
    config.reset_trusted_proxies()
    client = TestClient(
        app,
        client=("10.0.0.8", 12345),
        headers={"x-forwarded-for": "203.0.113.9"},
    )

    _fail_login(client)

    assert "203.0.113.9" in auth._LOGIN_FAILS
    assert "10.0.0.8" not in auth._LOGIN_FAILS


def test_login_ratelimit_without_proxy_uses_socket_peer(app):
    client = TestClient(
        app,
        client=("198.51.100.7", 12345),
        headers={"x-forwarded-for": "203.0.113.9"},
    )

    _fail_login(client)

    assert "198.51.100.7" in auth._LOGIN_FAILS
    assert "203.0.113.9" not in auth._LOGIN_FAILS
