"""QA 獨立驗證：任務 #4 /ws handler 內本機限定（依賴注入對 WS 不生效，於 handler 檢查）。

驗收要點（AC4 + AC5 之 WS 面）：
- /ws 對非本機來源會主動 close（不靠路由依賴），並送出「僅限本機存取」error。
- loopback peer → 放行，進入後續主流程（不因 loopback 被 close）。
- 受信代理偽造 XFF（真實 client 為公網）→ close。
- 來源不可知（預設 testclient，非 IP）→ fail-closed close。
- loopback 檢查『前置於』身分檢查：門禁啟用 + 公網來源 → 收到「僅限本機存取」而非「登入」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


def _first_event(client, headers=None):
    """連上 /ws 並取第一則伺服器訊息（loopback/登入 gate 會立即送 error 後 close）。"""
    with client.websocket_connect("/ws", headers=headers or {}) as ws:
        return ws.receive_json()


# --- AC4：公網來源 → 主動 close + 「僅限本機存取」-------------------------
def test_ws_public_peer_closed(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用也不得放行 WS
    client = TestClient(app, client=("203.0.113.5", 40000))
    ev = _first_event(client)
    assert ev["type"] == "error"
    assert ev["payload"]["message"] == "僅限本機存取"


# --- AC4：來源不可知（預設 testclient 非 IP）→ fail-closed close ----------
def test_ws_unknown_peer_fail_closed(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)  # client host = "testclient"，無法解析為 IP
    ev = _first_event(client)
    assert ev["type"] == "error"
    assert ev["payload"]["message"] == "僅限本機存取"


# --- AC5(WS)：受信代理偽造 XFF（真實 client 為公網）→ close ---------------
def test_ws_spoofed_xff_closed(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    monkeypatch.setenv("TI_TRUSTED_PROXIES", "127.0.0.0/8,::1")
    config.reset_trusted_proxies()
    try:
        client = TestClient(app, client=("127.0.0.1", 12345))  # peer 受信 → 採信 XFF
        # 最左塞 127.0.0.1，但最右非受信真實 client = 203.0.113.9（公網）
        ev = _first_event(client, headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.9"})
        assert ev["type"] == "error"
        assert ev["payload"]["message"] == "僅限本機存取"
    finally:
        config.reset_trusted_proxies()


# --- AC5(WS)：loopback peer → 放行，進入主流程（非「僅限本機存取」分支）----
def test_ws_loopback_peer_allowed_enters_flow(app, monkeypatch):
    """loopback 通過後進入主流程：送空需求 → 回「需求不可為空」（證明已過 loopback+auth gate）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用，loopback 直接進主流程
    client = TestClient(app, client=("127.0.0.1", 12345))
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": ""})  # 空需求觸發主流程內的驗證
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["payload"]["message"] != "僅限本機存取"  # 未被 loopback gate 擋
        assert "需求" in ev["payload"]["message"]  # 確實進入主流程


# --- AC4：loopback 檢查『前置於』身分檢查（順序與 HTTP 一致）-------------
def test_ws_loopback_precedes_auth(app, monkeypatch):
    """門禁啟用 + 公網來源：應先回 loopback 的「僅限本機存取」，而非「需要登入」。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")  # 門禁啟用
    client = TestClient(app, client=("203.0.113.5", 40000))  # 公網、未登入
    ev = _first_event(client)
    assert ev["type"] == "error"
    assert ev["payload"]["message"] == "僅限本機存取"
    assert "登入" not in ev["payload"]["message"]


# --- AC4：loopback 來源 + 門禁啟用未登入 → 落到登入分支（順序正確的反證）--
def test_ws_loopback_then_auth_gate(app, monkeypatch):
    """loopback 通過後，門禁啟用且未登入 → 回「需要登入」（證明兩道 gate 串接正確）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app, client=("127.0.0.1", 12345))  # loopback、未登入
    ev = _first_event(client)
    assert ev["type"] == "error"
    assert "登入" in ev["payload"]["message"]
