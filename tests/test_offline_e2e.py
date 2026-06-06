"""離線端到端：用假專家驅動真實的 server→orchestrator→runner 管線（真的寫檔/git/Demo）。

不需 API 金鑰；驗證整套機器（WebSocket、逐任務流程、自測、git commit、最終 Demo、
歷史存檔）實際運作。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app
    return TestClient(app)


def _run_session(client, requirement: str) -> list[dict]:
    events: list[dict] = []
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": requirement})
        for _ in range(500):  # 上限保護
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break
    return events


def test_offline_end_to_end(client):
    events = _run_session(client, "做一個會打招呼的程式")
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)

    # 流程完成且驗收通過
    assert "done" in by_type
    done = by_type["done"][-1]
    assert done["payload"]["completed"] is True
    sid = done["session_id"]

    # 真的寫出可執行程式碼
    assert "main.py" in workspace.list_files(sid)

    # 真的有階段性 git commit
    assert by_type.get("git_commit"), "應有 git commit 事件"

    # 最終 Demo 真的執行並輸出
    assert by_type.get("demo_result"), "應有 demo 結果"
    demo = by_type["demo_result"][-1]
    assert demo["payload"]["passed"] is True
    assert "Hello, Ti Studio!" in demo["payload"]["output"]

    # 看板最終把任務移到完成
    boards = by_type.get("board_update", [])
    assert boards and boards[-1]["payload"]["columns"]["done"]


def test_offline_history_recorded(client):
    events = _run_session(client, "需求 X")
    sid = events[-1]["session_id"]
    listed = client.get("/api/history").json()["sessions"]
    assert any(s["session_id"] == sid and s["status"] == "completed" for s in listed)
    replay = client.get(f"/api/history/{sid}/events").json()
    assert len(replay["events"]) == len(events)
