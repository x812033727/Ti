"""QA 驗證：/ws 改為「僅登入門禁、不限本機來源」後的行為。

政策變更（原任務 #4 把 /ws 鎖本機 → 現反轉）：
- /ws 是核心產品入口（啟動多專家討論）。對外網站須能讓已登入使用者開討論，
  故 **不再** 以 netutil.is_loopback 限定來源；安全模型改為「共用密碼門禁
  + 專家 bash 一律 bwrap 沙箱」。HTTP 管理類寫入仍維持 require_loopback（見
  test_qa_task2_loopback_writes / test_qa_task5_audit_table）。

驗收要點：
- 公網來源 + 門禁停用 → 進入主流程（不再被「僅限本機存取」擋）。
- 公網來源 + 門禁啟用、未登入 → 回「需要登入」（非「僅限本機存取」）。
- 公網來源 + 門禁啟用、已登入 → 進入主流程（外網登入即可開討論）。
- loopback 來源 → 一如既往進入主流程。
- 程式碼層級：ws handler 不再呼叫 netutil.is_loopback、亦不再送「僅限本機存取」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import auth, config


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


def _authed_headers() -> dict:
    """組一個合法登入 cookie 的 header（門禁啟用時用）。"""
    return {"Cookie": f"{config.AUTH_COOKIE}={auth.make_token()}"}


# --- 公網來源 + 門禁停用 → 不被本機限制擋，直接進主流程 -------------------
def test_ws_public_peer_not_blocked_by_loopback(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用
    client = TestClient(app, client=("203.0.113.5", 40000))  # 公網來源
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": ""})  # 空需求觸發主流程內驗證
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["payload"]["message"] != "僅限本機存取"  # 不再被本機 gate 擋
        assert "需求" in ev["payload"]["message"]  # 確實已進入主流程


# --- 公網來源 + 門禁啟用、未登入 → 「需要登入」（非「僅限本機存取」）------
def test_ws_public_peer_requires_auth_not_loopback(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")  # 門禁啟用
    client = TestClient(app, client=("203.0.113.5", 40000))  # 公網、未登入
    with client.websocket_connect("/ws") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert "登入" in ev["payload"]["message"]
        assert ev["payload"]["message"] != "僅限本機存取"


# --- 公網來源 + 門禁啟用、已登入 → 進入主流程（外網登入即可開討論）-------
def test_ws_public_peer_authed_enters_flow(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")  # 門禁啟用
    client = TestClient(app, client=("203.0.113.5", 40000))  # 公網來源
    with client.websocket_connect("/ws", headers=_authed_headers()) as ws:
        ws.send_json({"requirement": ""})  # 已登入 → 進主流程 → 空需求驗證
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["payload"]["message"] != "僅限本機存取"
        assert "需求" in ev["payload"]["message"]


# --- loopback 來源 + 門禁停用 → 一如既往進入主流程 -----------------------
def test_ws_loopback_peer_still_enters_flow(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app, client=("127.0.0.1", 12345))
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": ""})
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert "需求" in ev["payload"]["message"]


# --- 程式碼層級：/ws handler 不再做本機限定 ------------------------------
def test_ws_source_has_no_loopback_gate():
    import inspect

    from studio import ws as ws_mod

    src = inspect.getsource(ws_mod.ws)
    assert "is_loopback" not in src, "/ws 不應再呼叫 netutil.is_loopback"
    assert "僅限本機存取" not in src, "/ws 不應再送『僅限本機存取』"
