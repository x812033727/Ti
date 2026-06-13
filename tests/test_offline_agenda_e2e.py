"""任務 #5 冒煙：fake experts 走「需求→議程拆解→分派→逐子題討論→彙整」全流程（離線）。

用假專家驅動真實 server→ws→orchestrator 管線，引擎模式（TI_DISCUSS_MODE=round_robin）下驗證：
- agenda_plan 事件回指本場 fake PM 腳本的子題（自證對應，排除假綠）；
- assignee 硬驗證在真實管線生效：`負責: architect`（本場缺席）fallback engineer 且修正入事件；
- 逐子題討論真的發生（phase 事件＋討論期間 engineer/senior 有發言）；
- 彙整與既有流程零回歸（任務全完成、檔案落地、Demo 通過、history 可重看 agenda_plan）。
另附 legacy 反向對照：同腳本不開引擎模式，絕不出現逐子題討論 phase（證明非假綠）。
不需 API 金鑰。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, workspace

REQUIREMENT = "做一個四則運算 CLI"
# fake PM 腳本宣告的議程（fake_experts._pm_decompose_script 循序分支）——驗證輸出須回指這份輸入。
EXPECTED_TITLES = ["核心運算模組", "介面與說明"]


def _make_client(tmp_path, monkeypatch, discuss_mode: str) -> TestClient:
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "DISCUSS_MODE", discuss_mode)
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    # 與 test_offline_e2e 同款：學習機制 pin 關，驗證確定性產出。
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    monkeypatch.setattr(config, "SELF_REFINE_ITERS", 0)
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "0")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _make_client(tmp_path, monkeypatch, "round_robin")


@pytest.fixture
def legacy_client(tmp_path, monkeypatch):
    return _make_client(tmp_path, monkeypatch, "legacy")


def _run_session(client: TestClient, requirement: str) -> list[dict]:
    evs: list[dict] = []
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": requirement})
        for _ in range(800):  # 上限保護
            ev = ws.receive_json()
            evs.append(ev)
            if ev["type"] in ("done", "error"):
                break
    return evs


def _by_type(evs: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in evs:
        out.setdefault(e["type"], []).append(e)
    return out


def test_agenda_full_flow_engine_mode(client):
    evs = _run_session(client, REQUIREMENT)
    by_type = _by_type(evs)

    # 0) 全流程跑完且驗收通過（無未捕捉例外 → 不會收到 error）
    assert "error" not in by_type
    done = by_type["done"][-1]
    assert done["payload"]["completed"] is True
    sid = done["session_id"]

    # 1) 議程拆解＋分派：agenda_plan 事件回指 fake PM 腳本（自證對應）
    plans = by_type.get("agenda_plan", [])
    assert len(plans) == 1, "拆解後應 broadcast 恰一筆 agenda_plan"
    plan = plans[0]["payload"]
    assert [a["title"] for a in plan["agenda"]] == EXPECTED_TITLES
    # 子題標題確實出自本場 PM 的發言（排除假綠：不是 parser 憑空生出）
    pm_texts = [
        e["payload"]["text"] for e in by_type["expert_message"] if e["payload"]["speaker"] == "pm"
    ]
    assert all(any(t in txt for txt in pm_texts) for t in EXPECTED_TITLES)
    # 硬驗證：engineer 合法照分派；architect 本場缺席 → fallback engineer ＋修正紀錄
    assert [a["assignee"] for a in plan["assignments"]] == ["engineer", "engineer"]
    assert plan["corrections"] == [{"index": 1, "given": "architect", "assigned": "engineer"}]
    assert len(plan["tasks"]) == 3  # 任務清單同快照（沿用既有 parse_tasks）

    # 2) 逐子題討論真的發生：phase 事件＋兩個子題期間 engineer/senior 都有發言
    phases = [(e["payload"]["phase"], e["payload"]["detail"]) for e in by_type["phase_change"]]
    assert ("架構討論", "逐子題多角色討論（round_robin，2 個子題）") in phases
    idx = next(
        i
        for i, e in enumerate(evs)
        if e["type"] == "phase_change" and e["payload"]["phase"] == "架構討論"
    )
    nxt = next(i for i, e in enumerate(evs) if i > idx and e["type"] == "phase_change")
    speakers = [e["payload"]["speaker"] for e in evs[idx:nxt] if e["type"] == "expert_message"]
    # 2 子題 × 1 輪 × (主責 engineer ＋ senior) = engineer/senior 各 2 次發言
    assert speakers.count("engineer") == 2 and speakers.count("senior") == 2

    # 3) 彙整與既有流程零回歸：任務全完成、檔案落地、Demo 真的算出 7.0
    done_tasks = [e for e in by_type.get("task_status", []) if e["payload"]["status"] == "done"]
    assert len({e["payload"]["id"] for e in done_tasks}) == 3
    files = workspace.list_files(sid)
    assert {"calculator.py", "main.py", "README.md", "test_calculator.py"} <= set(files)
    demo = by_type["demo_result"][-1]
    assert demo["payload"]["passed"] is True and "7.0" in demo["payload"]["output"]

    # 4) 可重看：history 重播含同一筆 agenda_plan（議程/分派/修正俱在）
    replay = client.get(f"/api/history/{sid}/events").json()["events"]
    assert len(replay) == len(evs)
    saved = [e for e in replay if e["type"] == "agenda_plan"]
    assert len(saved) == 1 and saved[0]["payload"] == plan


def test_agenda_legacy_negative_control(legacy_client):
    """反向對照：同一份 fake PM 腳本、不開引擎模式——agenda_plan 照樣持久化（任務 #4），
    但絕不出現逐子題討論 phase（證明引擎 phase 事件非假綠）、流程零回歸跑完。"""
    evs = _run_session(legacy_client, REQUIREMENT)
    by_type = _by_type(evs)
    assert by_type["done"][-1]["payload"]["completed"] is True
    assert len(by_type.get("agenda_plan", [])) == 1
    details = [e["payload"]["detail"] for e in by_type["phase_change"]]
    assert not any("逐子題" in d for d in details)
