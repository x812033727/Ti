"""任務 #4：拆解結果（議程、任務、分派表）持久化進 history/session（離線測試）。

涵蓋：
- events.agenda_plan 建構子：type/payload 形狀、corrections/edges 預設、tuple 邊轉 list。
- full _run：拆解後 broadcast 一筆 AGENDA_PLAN，payload 回指本次 PM 輸出
  （議程標題、硬驗證後 assignee、修正紀錄、任務清單）——自證對應，排除假綠。
- history 落地：事件經 to_dict→record_event 寫入 jsonl 後，load_events 可查回
  議程/任務/分派表；finish_session 對新 event type 容錯（scorecard 推導不炸）。
全離線，不打真實 API。
"""

from __future__ import annotations

import pytest

from studio import config, events, history
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        return text

    async def stop(self) -> None:
        pass


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


PM_PLAN = (
    "子題: 資料層 | 設計儲存格式 | 可離線讀寫\n"
    "負責: senior\n"
    "子題: 介面層 | 設計 CLI 參數 | 一鍵可跑\n"
    "負責: ghost\n"
    "任務: #1 實作資料層\n"
    "任務: #2 實作介面層\n"
    "依賴: #2 -> #1\n"
    "執行指令: python main.py"
)


def _experts(pm_scripts):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts),
        "engineer": StubExpert(BY_KEY["engineer"], ["已實作"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }


# --- 建構子單元測試 --------------------------------------------------------


def test_agenda_plan_builder_payload_shape():
    agenda = [{"title": "A", "description": "d", "criteria": "c", "assignee": "senior"}]
    tasks = [{"id": 1, "title": "t", "status": "todo"}]
    assignments = [{"index": 1, "title": "A", "assignee": "senior"}]
    ev = events.agenda_plan(
        "s1",
        agenda,
        tasks,
        assignments,
        corrections=[{"index": 0, "given": "ghost", "assigned": "senior"}],
        edges=[(2, 1)],
    )
    assert ev.type == events.EventType.AGENDA_PLAN
    assert ev.type.value == "agenda_plan"
    d = ev.to_dict()
    assert d["type"] == "agenda_plan" and d["session_id"] == "s1"
    p = d["payload"]
    assert p["agenda"] == agenda
    assert p["tasks"] == tasks
    assert p["assignments"] == assignments
    assert p["corrections"] == [{"index": 0, "given": "ghost", "assigned": "senior"}]
    assert p["edges"] == [[2, 1]]  # tuple 邊序列化為 list（JSON 可寫）


def test_agenda_plan_builder_defaults():
    ev = events.agenda_plan("s1", [], [], [])
    assert ev.payload["corrections"] == [] and ev.payload["edges"] == []


# --- full _run：拆解後 broadcast 快照（回指輸入） ---------------------------


@pytest.mark.asyncio
async def test_run_broadcasts_agenda_plan_snapshot(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts([PM_PLAN, "決議: 完成", "檢討 OK"])
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("做一個記帳 CLI")

    plans = [e for e in bucket if e.type == events.EventType.AGENDA_PLAN]
    assert len(plans) == 1  # 一場一筆快照
    p = plans[0].payload
    # 議程回指本次 PM 輸出；assignee 已過硬驗證（ghost → engineer）
    assert [(a["title"], a["assignee"]) for a in p["agenda"]] == [
        ("資料層", "senior"),
        ("介面層", "engineer"),
    ]
    assert p["agenda"][0]["criteria"] == "可離線讀寫"
    # 分派表（1-based）與修正紀錄（0-based，對齊 validate_assignees）
    assert p["assignments"] == [
        {"index": 1, "title": "資料層", "assignee": "senior"},
        {"index": 2, "title": "介面層", "assignee": "engineer"},
    ]
    assert p["corrections"] == [{"index": 1, "given": "ghost", "assigned": "engineer"}]
    # 任務與依賴邊一併入快照
    assert [t["title"] for t in p["tasks"]] == ["實作資料層", "實作介面層"]
    assert p["edges"] == [[2, 1]]


# --- history 落地：record_event → load_events 可查回 ------------------------


@pytest.mark.asyncio
async def test_agenda_plan_persisted_into_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    sid = "agenda-hist"
    history.start_session(sid, "做一個記帳 CLI")
    bucket: list[dict] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        d = ev.to_dict()  # 與 ws.py broadcast 同路徑：to_dict 後 record_event
        bucket.append(d)
        history.record_event(sid, d)

    experts = _experts([PM_PLAN, "決議: 完成", "檢討 OK"])
    session = StudioSession(sid, broadcast, experts=experts, cwd=None)
    await session.run("做一個記帳 CLI")

    loaded = history.load_events(sid)
    plans = [e for e in loaded if e.get("type") == "agenda_plan"]
    assert len(plans) == 1
    p = plans[0]["payload"]
    assert [a["title"] for a in p["agenda"]] == ["資料層", "介面層"]
    assert p["assignments"][0]["assignee"] == "senior"
    assert p["corrections"] == [{"index": 1, "given": "ghost", "assigned": "engineer"}]
    # finish_session 的 scorecard/status 推導對新 type 容錯不炸
    meta = history.finish_session(sid)
    assert meta is not None and meta["n_events"] == len(loaded)
