"""Session 軟性時間預算：撞 autopilot 硬 timeout 前主動收斂，優雅出貨已完成成果而非整場被砍。

治本 8 場「timeout after 3600s」——那些是討論一路忙到 wall-clock 牆、被 autopilot 的 wait_for 硬砍、
連已完成的任務都丟成 failed。改為:session 在硬 timeout 的 SESSION_SOFT_DEADLINE_FRAC 比例處停止派發
新任務,已完成的續走 Demo/出貨,未動的記 known-limit/followup。
"""

from __future__ import annotations

import pytest

from studio import config, events
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
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


# --- _time_exceeded 純計時邏輯 -----------------------------------------


def test_time_exceeded_threshold(monkeypatch):
    """過 budget×frac 才回 True；與 _stop 分離；觸發置 _deadline_hit。"""
    from studio import orchestrator

    clock = {"t": 1000.0}
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(config, "SESSION_SOFT_DEADLINE_FRAC", 0.85)

    s = StudioSession("t", lambda e: None, cwd=None, time_budget_s=100)
    s._t0_run = clock["t"]  # 開工基準
    assert s._time_exceeded() is False  # 0 秒
    clock["t"] = 1084.0
    assert s._time_exceeded() is False  # 84 < 85
    assert s._deadline_hit is False
    clock["t"] = 1085.0
    assert s._time_exceeded() is True  # 85 >= 85
    assert s._deadline_hit is True
    assert s._stop is False  # 時間到 != 中止，仍可出貨


def test_time_exceeded_disabled_without_budget(monkeypatch):
    """無 time_budget_s（手動討論）→ 永不觸發、行為與舊版一致。"""
    from studio import orchestrator

    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: 9e9)
    s = StudioSession("t", lambda e: None, cwd=None, time_budget_s=None)
    s._t0_run = 0.0
    assert s._time_exceeded() is False
    assert s._deadline_hit is False


# --- 整合：截斷後優雅收尾 ----------------------------------------------


@pytest.mark.asyncio
async def test_deadline_truncates_remaining_tasks_and_wraps_up(monkeypatch):
    """過軟性預算後不再派發新任務:已完成的保留,未動的留 todo,發「時間預算收斂」事件,正常回傳結果。"""
    bucket, broadcast = collect()
    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: A\n任務: B", "決議: 完成", "檢討"]),
        "engineer": StubExpert(BY_KEY["engineer"], ["做好了"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, time_budget_s=100)

    # 前 2 次檢查（wave 入口、任務 A）放行；第 3 次起（任務 B）截斷。
    calls = {"n": 0}

    def fake_exceeded():
        calls["n"] += 1
        if calls["n"] >= 3:
            session._deadline_hit = True
            return True
        return False

    monkeypatch.setattr(session, "_time_exceeded", fake_exceeded)

    result = await session.run("需求")

    # 正常回傳結果（沒有拋 TimeoutError、沒有整場崩）
    assert isinstance(result, dict)
    # 任務 A 完成、任務 B 未動（留 todo → unmet → known-limit）
    by_id = {t["id"]: t for t in session._tasks}
    assert by_id[1]["status"] == "done"
    assert by_id[2]["status"] != "done"
    # 發出可觀察的「時間預算收斂」事件
    phases = [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "時間預算收斂" in phases
    # 未全數完成 → 不謊報全完成
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is False


# --- autopilot 接線：把硬 timeout 當軟性預算傳進 session --------------


@pytest.mark.asyncio
async def test_autopilot_passes_time_budget(monkeypatch, tmp_path):
    from studio import autopilot

    clone = tmp_path / "clone"
    clone.mkdir()
    captured = {}

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def run(self, _requirement):
            return {
                "completed": False,
                "shippable": False,
                "followups": [],
                "followup_items": [],
                "core_changes": [],
            }

    monkeypatch.setattr(config, "AUTOPILOT_TASK_TIMEOUT", 3600)
    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", FakeSession)
    monkeypatch.setattr(autopilot.history, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "finish_session", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "set_status", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "add_items", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add_many", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *a, **k: 0)

    await autopilot.run_one_task({"id": 1, "title": "x"})

    assert captured.get("time_budget_s") == 3600
