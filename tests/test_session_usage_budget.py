"""Session 每場用量預算（成本熔斷）：與時間預算同機制，累計 token／USD 達上限即停止派發新任務、
優雅收尾出貨，治「失控場一路燒 token 到撞硬 timeout」。

對照 test_session_time_budget.py——時間預算驅動自時鐘，本檔驅動自累進的 token_usage 事件。
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


class TokenBurningEngineer(StubExpert):
    """工程師發言後額外送一筆大額 token_usage，模擬「實作這一輪就燒掉預算」，驅動真實 _budget_exceeded。"""

    def __init__(self, role: Role, scripts: list[str], total_tokens: int, cost_usd: float = 0.0):
        super().__init__(role, scripts)
        self._total = total_tokens
        self._cost = cost_usd

    async def speak(self, prompt: str, broadcast) -> str:
        r = await super().speak(prompt, broadcast)
        await broadcast(
            events.token_usage(
                "t",
                self.role.key,
                "claude",
                "claude-opus-4-8",
                self._total,
                0,
                self._total,
                cost_usd=self._cost or None,
            )
        )
        return r


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


# --- _counting_broadcast 累計 -------------------------------------------


@pytest.mark.asyncio
async def test_counting_broadcast_accumulates_tokens_and_cost():
    bucket, sink = collect()
    s = StudioSession("t", sink, cwd=None)
    # token_usage 事件 → 累進；其餘事件只透傳不計數。
    await s.broadcast(events.token_usage("t", "pm", "claude", "m", 100, 20, 120, cost_usd=0.5))
    await s.broadcast(events.expert_message("t", "pm", "PM", "🧭", "一般發言"))
    await s.broadcast(events.token_usage("t", "engineer", "claude", "m", 30, 4, 34, cost_usd=0.1))
    assert s._tokens_used == 154
    assert s._usd_used == pytest.approx(0.6)
    # 事件仍原樣轉送下游 sink（含非 token 事件）
    assert len(bucket) == 3


@pytest.mark.asyncio
async def test_counting_broadcast_aggregates_task_perf_by_task_id():
    bucket, sink = collect()
    s = StudioSession("t", sink, cwd=None)

    await s.broadcast(
        events.token_usage("t", "engineer", "claude", "m", 100, 20, 120, cost_usd=0.5, task_id=1)
    )
    await s.broadcast(
        events.token_usage("t", "qa", "claude", "m", 10, 5, 15, cost_usd=0.1, task_id=2)
    )
    await s.broadcast(
        events.token_usage("t", "engineer", "claude", "m", 30, 4, 34, cost_usd=0.2, task_id=1)
    )
    await s.broadcast(events.token_usage("t", "pm", "claude", "m", 999, 1, 1000))

    assert s._task_perf[1]["input_tokens"] == 130
    assert s._task_perf[1]["output_tokens"] == 24
    assert s._task_perf[1]["total_tokens"] == 154
    assert s._task_perf[1]["cost_usd"] == pytest.approx(0.7)
    assert s._task_perf[1]["cost_source"] == "reported"

    assert s._task_perf[2]["input_tokens"] == 10
    assert s._task_perf[2]["output_tokens"] == 5
    assert s._task_perf[2]["total_tokens"] == 15
    assert s._task_perf[2]["cost_usd"] == pytest.approx(0.1)
    assert s._task_perf[2]["cost_source"] == "reported"

    assert set(s._task_perf) == {1, 2}
    assert len(bucket) == 4


@pytest.mark.asyncio
async def test_counting_broadcast_keeps_unknown_task_cost_none():
    _bucket, sink = collect()
    s = StudioSession("t", sink, cwd=None)

    await s.broadcast(events.token_usage("t", "engineer", "minimax", "m", 8, 2, 10, task_id=3))

    assert s._task_perf[3]["input_tokens"] == 8
    assert s._task_perf[3]["output_tokens"] == 2
    assert s._task_perf[3]["total_tokens"] == 10
    assert s._task_perf[3]["cost_usd"] is None
    assert s._task_perf[3]["cost_source"] is None


# --- _budget_exceeded 門檻邏輯 ------------------------------------------


def test_budget_exceeded_by_token(monkeypatch):
    monkeypatch.setattr(config, "SESSION_TOKEN_BUDGET", 1000)
    monkeypatch.setattr(config, "SESSION_USD_BUDGET", 0)
    s = StudioSession("t", lambda e: None, cwd=None)
    s._tokens_used = 999
    assert s._budget_exceeded() is False
    assert s._deadline_hit is False
    s._tokens_used = 1000
    assert s._budget_exceeded() is True
    assert s._deadline_hit is True and s._budget_hit is True
    assert s._stop is False  # 用量到 != 中止，仍可優雅出貨


def test_budget_exceeded_by_usd(monkeypatch):
    monkeypatch.setattr(config, "SESSION_TOKEN_BUDGET", 0)
    monkeypatch.setattr(config, "SESSION_USD_BUDGET", 2.0)
    s = StudioSession("t", lambda e: None, cwd=None)
    s._usd_used = 1.99
    assert s._budget_exceeded() is False
    s._usd_used = 2.0
    assert s._budget_exceeded() is True
    assert s._budget_hit is True


def test_budget_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "SESSION_TOKEN_BUDGET", 0)
    monkeypatch.setattr(config, "SESSION_USD_BUDGET", 0)
    s = StudioSession("t", lambda e: None, cwd=None)
    s._tokens_used = 10**9
    s._usd_used = 10**6
    assert s._budget_exceeded() is False
    assert s._deadline_hit is False


def test_should_wind_down_combines_time_and_budget(monkeypatch):
    monkeypatch.setattr(config, "SESSION_TOKEN_BUDGET", 100)
    monkeypatch.setattr(config, "SESSION_USD_BUDGET", 0)
    s = StudioSession("t", lambda e: None, cwd=None, time_budget_s=None)
    s._t0_run = 0.0
    # 兩者皆未觸發
    assert s._should_wind_down() is False
    # 只有用量觸發
    s._tokens_used = 100
    assert s._should_wind_down() is True


# --- 整合：token_usage 累進驅動真實熔斷，測截斷後優雅收尾 ---------------


@pytest.mark.asyncio
async def test_budget_truncates_remaining_tasks_and_wraps_up(monkeypatch):
    """過用量預算後不再派發新任務：已完成的保留、未動的留 todo、發「用量預算收斂」事件、正常回傳。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)  # 序列化兩任務，判定確定性
    monkeypatch.setattr(config, "SESSION_TOKEN_BUDGET", 1000)
    monkeypatch.setattr(config, "SESSION_USD_BUDGET", 0)

    bucket, broadcast = collect()
    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: A\n任務: B", "決議: 完成", "檢討"]),
        # 第一個任務實作就燒掉 5000 token（> 1000 預算）→ 下一波派發前熔斷。
        "engineer": TokenBurningEngineer(BY_KEY["engineer"], ["做好了"], total_tokens=5000),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),  # 任務 A 第一輪即過
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None)

    result = await session.run("需求")

    assert isinstance(result, dict)
    # 過預算後不再派發新任務 → 至少一個任務被截斷未完成
    statuses = [t["status"] for t in session._tasks]
    assert statuses.count("done") < len(session._tasks)
    phases = [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "用量預算收斂" in phases
    assert "時間預算收斂" not in phases  # 觸發的是用量而非時間
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is False
    # 確實有累計到 token（熔斷不是誤觸）
    assert session._tokens_used >= 1000
