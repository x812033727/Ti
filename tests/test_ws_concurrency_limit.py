"""測試 /ws 並發上限（TI_MAX_CONCURRENT_SESSIONS）：slot 取得/釋放、超限拒絕、結束釋放。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from studio import config, ws


@pytest.fixture(autouse=True)
def _reset_slots(monkeypatch):
    # 每測試起始 slot 歸零，避免互相污染（monkeypatch 於 teardown 還原）。
    monkeypatch.setattr(ws, "_active_sessions", 0)


# --- slot 取得/釋放邏輯 -------------------------------------------------
def test_acquire_release_respects_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 2)
    assert ws._acquire_session_slot() is True
    assert ws._acquire_session_slot() is True
    assert ws._acquire_session_slot() is False  # 達上限
    ws._release_session_slot()
    assert ws._acquire_session_slot() is True  # 釋放一個後可再取


def test_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 0)
    for _ in range(50):
        assert ws._acquire_session_slot() is True


def test_release_clamps_at_zero():
    ws._release_session_slot()  # 不得變負
    assert ws._active_sessions == 0


# --- /ws 超限拒絕（整合）-----------------------------------------------
def test_ws_rejects_when_at_limit(tmp_path, monkeypatch):
    from studio.server import app

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)  # 略過 provider_ready 檢查
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 1)
    monkeypatch.setattr(ws, "_active_sessions", 1)  # 已達上限
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    client = TestClient(app, client=("127.0.0.1", 12345))
    with client.websocket_connect("/ws") as conn:
        conn.send_json({"requirement": "做個東西"})
        msg = conn.receive_json()
    assert msg["type"] == "error"
    assert "上限" in msg["payload"]["message"]
    assert ws._active_sessions == 1  # 被拒：未占用、也未誤釋放


# --- 正常跑完一場後 slot 釋放（整合，驗證 run_task done-callback 接線）----
def test_ws_releases_slot_after_session(tmp_path, monkeypatch):
    from studio.server import app

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 4)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    client = TestClient(app, client=("127.0.0.1", 12345))
    with client.websocket_connect("/ws") as conn:
        conn.send_json({"requirement": "做一個 BMI CLI"})
        for _ in range(3000):  # 收到 done/error 即止（避免無限等待）
            ev = conn.receive_json()
            if ev["type"] in ("done", "error"):
                break
    # 跑完後 slot 應釋放回 0（run_task done-callback 觸發；給極短時間讓 callback 跑完）
    for _ in range(100):
        if ws._active_sessions == 0:
            break
        time.sleep(0.02)
    assert ws._active_sessions == 0
