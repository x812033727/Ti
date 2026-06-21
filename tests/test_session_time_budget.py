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


# --- 整合：以可控時鐘驅動真實 _time_exceeded，測截斷後優雅收尾 ----------


class ClockEngineer(StubExpert):
    """工程師發言後把時鐘推過軟性預算（模擬「實作這一輪就耗掉預算」），驅動真實 _time_exceeded。"""

    def __init__(self, role: Role, scripts: list[str], clock: dict):
        super().__init__(role, scripts)
        self._clock = clock

    async def speak(self, prompt: str, broadcast) -> str:
        r = await super().speak(prompt, broadcast)
        self._clock["t"] = 10_000.0  # 跳過 time_budget_s × frac
        return r


def _deadline_after_first_impl(session, clock):
    """把 _time_exceeded 換成「時鐘過 850（=1000×0.85）即觸發」的判定（instance 屬性，判定確定）。

    刻意換掉 _time_exceeded 本體而非 patch time.monotonic：本測只驗「orchestrator 有在這些點
    *呼叫* deadline 檢查」（_work_task 每輪頂、huddle 前、派發邊界）——計時邏輯本身另由
    test_time_exceeded_threshold 驗。ClockEngineer 在每輪實作後把時鐘推過門檻來驅動。
    """

    def fake():
        if clock["t"] >= 850:
            session._deadline_hit = True
            return True
        return False

    return fake


@pytest.mark.asyncio
async def test_deadline_truncates_remaining_tasks_and_wraps_up(monkeypatch):
    """過軟性預算後不再派發新任務:已完成的保留,未動的留 todo,發「時間預算收斂」事件,正常回傳結果。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)  # 序列化兩任務,判定確定性

    clock = {"t": 0.0}
    bucket, broadcast = collect()
    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: A\n任務: B", "決議: 完成", "檢討"]),
        "engineer": ClockEngineer(BY_KEY["engineer"], ["做好了"], clock),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),  # 任務 A 第一輪即過
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, time_budget_s=1000)
    monkeypatch.setattr(session, "_time_exceeded", _deadline_after_first_impl(session, clock))

    result = await session.run("需求")

    # 正常回傳結果（沒有拋 TimeoutError、沒有整場崩）
    assert isinstance(result, dict)
    # 過預算後不再派發新任務 → 至少一個任務被截斷未完成（留 todo → unmet → known-limit）
    statuses = [t["status"] for t in session._tasks]
    assert statuses.count("done") < len(session._tasks)
    phases = [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "時間預算收斂" in phases
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is False


# --- 任務「內部」迴圈也要受時間預算約束（#217 後驗證補洞）-------------


@pytest.mark.asyncio
async def test_deadline_breaks_intra_task_loop_and_skips_huddle(monkeypatch):
    """時間多半耗在單任務的多輪迴圈裡：過預算須在 _work_task 每輪中止、且不再開 huddle。

    回歸 #217 後驗證抓到的破口——core #27 卡在單任務迴圈、deadline 只在派發邊界檢查而漏掉、
    撐到硬 timeout。修法:_work_task 每輪頂 + huddle 進入前都檢查 _time_exceeded()。
    """
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)  # 三輪:沒有「每輪檢查」時三輪全跑→3 次實作

    clock = {"t": 0.0}
    bucket, broadcast = collect()
    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: 只有一個", "決議: 完成", "檢討"]),
        # 每輪實作後時鐘跳過預算;qa 每輪都 FAIL → 無「每輪檢查」會跑滿 3 輪再 huddle。
        "engineer": ClockEngineer(BY_KEY["engineer"], ["R1", "R2", "R3", "R4"], clock),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: FAIL"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 退回"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, time_budget_s=1000)
    monkeypatch.setattr(session, "_time_exceeded", _deadline_after_first_impl(session, clock))

    result = await session.run("需求")

    assert isinstance(result, dict)
    # 迴圈在過預算後的某一輪頂端中止 → 不會跑滿 3 輪（無此修正會是 3 次實作）。
    assert experts["engineer"].calls <= 2
    # huddle（卡關討論）不應被召開（huddle 進入前的 deadline 守衛）
    phases = [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "卡關討論" not in phases
    # 仍優雅收尾、發收斂事件、不謊報完成
    assert "時間預算收斂" in phases
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is False


@pytest.mark.asyncio
async def test_deadline_skips_review_fanout_within_round(monkeypatch):
    """過軟性預算落在「本輪實作後、三審前」→ 跳過昂貴的 QA/senior fan-out、提早收尾。

    補 #217 盲點:每輪「頂端」檢查（round-top）擋不住「單輪本身超長」的稽核型任務——reviewer
    fan-out（commit 後）在軟 deadline 之後才開始就會一路撐到硬 timeout、整場記 timeout failed
    （autopilot #83:3060s 過軟 deadline 後仍跑滿到 3600s 被砍）。修法:commit 後、三審前再檢查
    _time_exceeded()。

    序列化單任務（PARALLEL 關）下，_time_exceeded 的呼叫點固定為:#1 wave 派發、#2 lane 派發、
    #3 _work_task 輪頂、#4 commit 後、三審前（本 fix 新增）。故前 3 次回 False（讓 _work_task
    正常進入並實作 commit）、第 4 次回 True，精準落在本檢查點。
    """
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)

    bucket, broadcast = collect()
    engineer = StubExpert(BY_KEY["engineer"], ["R1", "R2"])
    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: 只有一個", "決議: 完成", "檢討"]),
        "architect": StubExpert(BY_KEY["architect"], ["架構意見"]),
        "engineer": engineer,
        # QA 會回 PASS;若 fan-out 沒被跳過、本輪就會過審完成,calls 也會 >0。
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, time_budget_s=1000)

    state = {"n": 0}

    def fake_exceeded() -> bool:
        state["n"] += 1
        hit = state["n"] >= 4  # 前 3 次（wave/lane/輪頂）False、第 4 次（三審前）起 True
        if hit:
            session._deadline_hit = True
        return hit

    monkeypatch.setattr(session, "_time_exceeded", fake_exceeded)

    result = await session.run("需求")

    assert isinstance(result, dict)
    # 三審 fan-out 被跳過:QA 只在 _work_task 的審查階段被呼叫,過 deadline 跳過後 calls 應為 0
    # （senior 不能拿來斷言——它在「驗收」收尾階段仍會被呼叫一次,非本輪 fan-out）。
    assert experts["qa"].calls == 0, "過軟預算後不應再跑 QA 審查 fan-out"
    # 發「時間預算收尾」（本輪內截斷的專屬事件,有別於 session 邊界的「時間預算收斂」）。
    phases = [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "時間預算收尾" in phases
    # 不謊報完成。
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
