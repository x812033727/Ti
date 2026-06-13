"""討論小組接通 orchestrator：選用 group 後，架構討論改以小組成員＋小組 mode 進行。

涵蓋（全離線、不打真實 API）：
- _group_participants：role_keys 解析成 (mode, 成員)、實例去重、可解析 <2 名退回預設班底。
- _discuss_agenda：選用小組時班底＝小組成員、mode 蓋過全域 DISCUSS_MODE、主責 proposer-first。
- run() 分流：選了小組即使 DISCUSS_MODE=legacy 也走逐子題討論（小組優先於預設/逃生口路徑）。
"""

from __future__ import annotations

import pytest

from studio import config, events
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    def __init__(self, role: Role, scripts: list[str], order: list | None = None):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []
        self.order = order

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        if self.order is not None:
            self.order.append(self.role.key)
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


def phases(bucket):
    return [
        (e.payload["phase"], e.payload["detail"])
        for e in bucket
        if e.type == events.EventType.PHASE_CHANGE
    ]


def _experts(pm_scripts, order=None):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts, order),
        "engineer": StubExpert(BY_KEY["engineer"], ["已實作"], order),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"], order),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"], order),
    }


def _session(broadcast, agenda, experts, group=None):
    s = StudioSession("t", broadcast, experts=experts, cwd=None, group=group)
    s._agenda = agenda
    s._main_ctx = LaneContext(lane_id="main", cwd=None, experts={})
    return s


PM_PLAN_WITH_AGENDA = (
    "子題: 資料層 | 設計儲存格式 | 可離線讀寫\n"
    "負責: senior\n"
    "子題: 介面層 | 設計 CLI 參數 | 一鍵可跑\n"
    "負責: engineer\n"
    "任務: #1 實作資料層\n"
    "執行指令: python main.py"
)


# --- _group_participants 純解析 ------------------------------------------


def test_group_participants_resolves_members_and_mode():
    experts = _experts(["x"])
    group = {"name": "設計組", "role_keys": ["qa", "engineer", "senior"], "mode": "round_robin"}
    session = StudioSession("t", lambda ev: None, experts=experts, cwd=None, group=group)
    resolved = session._group_participants(experts)
    assert resolved is not None
    mode, members = resolved
    assert mode == "round_robin"
    # 順序沿用 role_keys；名稱取 expert.role.name；不含 pm。
    assert [name for name, _ in members] == [
        BY_KEY["qa"].name,
        BY_KEY["engineer"].name,
        BY_KEY["senior"].name,
    ]


def test_group_participants_dedup_same_expert_instance():
    experts = _experts(["x"])
    # 同一 key 重複（驗證寫入端理論不允許，但解析端仍以實例去重防呆）。
    group = {"name": "組", "role_keys": ["qa", "qa", "engineer"], "mode": "parallel"}
    session = StudioSession("t", lambda ev: None, experts=experts, cwd=None, group=group)
    _, members = session._group_participants(experts)
    assert [name for name, _ in members] == [BY_KEY["qa"].name, BY_KEY["engineer"].name]


def test_group_participants_fallback_when_under_two():
    experts = _experts(["x"])
    # 成員都不在本場出席角色 → 可解析 <2 → 退回 None（用預設班底）。
    group = {"name": "幽靈組", "role_keys": ["ghost1", "ghost2"], "mode": "parallel"}
    session = StudioSession("t", lambda ev: None, experts=experts, cwd=None, group=group)
    assert session._group_participants(experts) is None


# --- _discuss_agenda 採用小組班底＋mode ------------------------------------


@pytest.mark.asyncio
async def test_discuss_agenda_uses_group_roster_and_mode(monkeypatch):
    """小組 mode 蓋過全域 DISCUSS_MODE=legacy；主責（assignee）proposer-first；pm 不在組不發言。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "legacy")
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 1)
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    order: list[str] = []
    experts = _experts(["x"], order=order)
    group = {"name": "設計組", "role_keys": ["qa", "engineer", "senior"], "mode": "round_robin"}
    agenda = [{"title": "唯一子題", "description": "", "criteria": "", "assignee": "senior"}]
    bucket, broadcast = collect()
    session = _session(broadcast, agenda, experts, group=group)
    await session._discuss_agenda(experts, experts["engineer"], experts["senior"], "需求")

    # round_robin（小組 mode，非 legacy）；主責 senior 排首位 → senior, qa, engineer。
    assert order == ["senior", "qa", "engineer"]
    assert experts["pm"].calls == 0  # pm 不在小組，不發言
    assert any(
        p == "架構討論" and "round_robin" in d and "討論小組「設計組」（3 人）" in d
        for p, d in phases(bucket)
    )
    assert "主責: 高級工程師" in experts["senior"].prompts[0]


@pytest.mark.asyncio
async def test_discuss_agenda_fallback_to_default_roster(monkeypatch):
    """小組成員都不在場 → 退回預設班底（assignee＋eng＋senior），phase 不標小組。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "round_robin")
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 1)
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    order: list[str] = []
    experts = _experts(["x"], order=order)
    group = {"name": "幽靈組", "role_keys": ["ghost1", "ghost2"], "mode": "parallel"}
    agenda = [{"title": "唯一子題", "description": "", "criteria": "", "assignee": "engineer"}]
    bucket, broadcast = collect()
    session = _session(broadcast, agenda, experts, group=group)
    await session._discuss_agenda(experts, experts["engineer"], experts["senior"], "需求")

    # 預設班底：engineer（主責）＋senior（pm/qa 不在班底）。
    assert set(order) == {"engineer", "senior"}
    assert not any("討論小組" in d for _, d in phases(bucket))


# --- run() 分流：小組優先於 legacy 逃生口 ----------------------------------


@pytest.mark.asyncio
async def test_run_group_overrides_legacy_dispatch(monkeypatch):
    """DISCUSS_MODE=legacy 時，選了小組仍走逐子題討論（非兩人 _debate），且用小組 mode。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "legacy")
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 1)
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    bucket, broadcast = collect()
    experts = _experts([PM_PLAN_WITH_AGENDA, "決議: 完成", "檢討 OK"])
    group = {"name": "全員組", "role_keys": ["engineer", "senior", "qa"], "mode": "round_robin"}
    session = StudioSession("t", broadcast, experts=experts, cwd=None, group=group)
    await session.run("做一個記帳 CLI")

    archi = [d for p, d in phases(bucket) if p == "架構討論"]
    assert any("討論小組「全員組」（3 人）" in d and "round_robin" in d for d in archi)
    # 子題 topic 進到討論 prompt（自證回指本次 PM 輸出）。
    assert any("議程子題 1/2：資料層" in pr for pr in experts["qa"].prompts)
