"""離線端到端：用假專家把「動態優先」流程真正跑過一遍 server→orchestrator→runner。

驗證 dynamic-first（含 session 級 dynamic stage）端到端可收斂完成——不只單元測各零件，
而是整條 _run_workflow 直譯器 + _stage_dynamic + build + demo + wrap_up 實際走通。不需金鑰。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    monkeypatch.setattr(config, "SELF_REFINE_ITERS", 0)
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "0")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def _run(client, requirement: str, workflow: str) -> list[dict]:
    events: list[dict] = []
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": requirement, "workflow": workflow})
        for _ in range(800):  # 動態 stage 多走幾 hop，上限放寬
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break
    return events


def test_dynamic_first_runs_end_to_end(client):
    events = _run(client, "做一個四則運算 CLI", workflow="動態優先")
    by_type: dict[str, list] = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)

    # 沒有 error；流程跑到 done
    assert "error" not in by_type, f"不應有 error：{by_type.get('error')}"
    assert "done" in by_type, "動態優先流程應跑到 done"

    # 開場 WORKFLOW_PLAN 顯示採用的是「動態優先」
    wp = by_type.get("workflow_plan", [])
    assert wp and wp[0]["payload"]["name"] == "動態優先"
    plan_types = [s["type"] for s in wp[0]["payload"]["stages"]]
    assert "dynamic" in plan_types, "動態優先的 stage 序列應含 session 級 dynamic"

    # session 級 dynamic stage 真的有跑（phase_change「動態溝通與分派」）
    phases = [e["payload"].get("phase") for e in by_type.get("phase_change", [])]
    assert "動態溝通與分派" in phases, f"應有動態溝通階段，實際 phases：{phases}"

    # 仍走完任務波次與最終 Demo（驗證），且實際寫出檔案
    assert by_type.get("demo_result"), "應有最終 Demo（驗證）"
    sid = by_type["done"][-1]["session_id"]
    from studio import workspace

    assert workspace.list_files(sid), "應有實際寫出的檔案"
