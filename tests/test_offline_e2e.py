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
    # 停用門禁，讓 e2e 不受環境變數 TI_ACCESS_PASSWORD 影響（否則 WS 握手會被擋）。
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app

    # /ws 已限定本機（handler 內 is_loopback 檢查）：以 loopback peer 連入握手。
    return TestClient(app, client=("127.0.0.1", 12345))


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
    events = _run_session(client, "做一個四則運算 CLI")
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)

    # 流程完成且驗收通過
    assert "done" in by_type
    done = by_type["done"][-1]
    assert done["payload"]["completed"] is True
    sid = done["session_id"]

    # 逐任務寫出多個真實檔案
    files = workspace.list_files(sid)
    assert {"calculator.py", "main.py", "README.md", "test_calculator.py"} <= set(files)

    # 真的有階段性 git commit
    assert by_type.get("git_commit"), "應有 git commit 事件"

    # 三個任務都移到完成
    done_tasks = [e for e in by_type.get("task_status", []) if e["payload"]["status"] == "done"]
    assert len({e["payload"]["id"] for e in done_tasks}) == 3

    # 最終 Demo 真的執行四則運算並輸出 7.0
    assert by_type.get("demo_result"), "應有 demo 結果"
    demo = by_type["demo_result"][-1]
    assert demo["payload"]["passed"] is True
    assert "7.0" in demo["payload"]["output"]

    # 看板最終把任務移到完成
    boards = by_type.get("board_update", [])
    assert boards and boards[-1]["payload"]["columns"]["done"]


def test_offline_shows_internal_discussion(client):
    """離線端到端：流程跑完，且至少出現一次「內部討論」事件（critic 或 huddle）。"""
    events = _run_session(client, "做一個四則運算 CLI")
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)

    # 流程跑完並達標
    assert by_type["done"][-1]["payload"]["completed"] is True
    # 至少一次內部討論事件
    discussions = by_type.get("critic_review", []) + by_type.get("huddle", [])
    assert discussions, "離線示範應展示至少一次內部討論事件（critic_review/huddle）"
    # critic 在主路徑放行（不阻斷流程）
    critics = by_type.get("critic_review", [])
    assert critics and any(e["payload"]["passed"] for e in critics)


def test_offline_history_recorded(client):
    events = _run_session(client, "需求 X")
    sid = events[-1]["session_id"]
    listed = client.get("/api/history").json()["sessions"]
    assert any(s["session_id"] == sid and s["status"] == "completed" for s in listed)
    replay = client.get(f"/api/history/{sid}/events").json()
    assert len(replay["events"]) == len(events)
