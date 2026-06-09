"""QA 驗收：任務 #6「離線可 demo」驗收標準專測。

驗收標準：
- TI_OFFLINE=1 端到端流程能跑完（completed=True）。
- 過程中展示至少一次「內部討論」事件（huddle 或 critic_review）。
補充：
- 離線示範由 OFFLINE_MODE 自動啟用 critic（不依賴 TI_CRITIC 預設值/環境變數）。
- 內部討論事件會寫進 history，可被前端重播呈現。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    # 刻意保持新機制開關為「關閉」，證明離線 demo 不依賴它們的預設值。
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app

    # /ws 已限定本機（handler 內 is_loopback 檢查）：以 loopback peer 連入握手。
    return TestClient(app, client=("127.0.0.1", 12345))


def _run(client, requirement: str) -> list[dict]:
    events: list[dict] = []
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": requirement})
        for _ in range(800):
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break
    return events


def _by_type(events):
    out: dict[str, list[dict]] = {}
    for e in events:
        out.setdefault(e["type"], []).append(e)
    return out


def test_offline_completes_and_shows_discussion(client):
    """端到端跑完 + 至少一次內部討論事件（即使所有開關預設關閉）。"""
    events = _run(client, "做一個四則運算 CLI")
    by_type = _by_type(events)

    # 1) 流程跑完且達標
    assert "done" in by_type
    assert by_type["done"][-1]["payload"]["completed"] is True

    # 2) 至少一次內部討論事件（huddle 或 critic_review）
    discussions = by_type.get("critic_review", []) + by_type.get("huddle", [])
    assert discussions, "離線 demo 應展示至少一次內部討論事件"


def test_offline_auto_enables_critic_without_env_flag(client):
    """CRITIC_ENABLED=False 下，OFFLINE_MODE 仍自動啟用 critic 關卡並發事件。"""
    assert config.CRITIC_ENABLED is False  # 確認沒靠開關
    events = _run(client, "做一個 BMI 計算器")
    by_type = _by_type(events)

    critics = by_type.get("critic_review", [])
    assert critics, "離線示範應自動跑 critic（不需 TI_CRITIC）"
    # critic 在主路徑放行，不阻斷流程
    assert any(e["payload"]["passed"] for e in critics)
    assert by_type["done"][-1]["payload"]["completed"] is True
    # 換人原則：至少出現任務審查(pm)或最終驗收(senior)其一視角
    gates = {e["payload"]["gate"] for e in critics}
    assert gates & {"pm", "senior"}


def test_discussion_event_is_replayable_from_history(client):
    """內部討論事件寫進 history，可被前端重播 API 取回（demo 可重現）。"""
    events = _run(client, "做一個待辦清單 CLI")
    sid = events[-1]["session_id"]

    replay = client.get(f"/api/history/{sid}/events").json()["events"]
    replay_types = {e["type"] for e in replay}
    assert "critic_review" in replay_types or "huddle" in replay_types
    # 重播事件數與即時收到的一致（完整存檔）
    assert len(replay) == len(events)
