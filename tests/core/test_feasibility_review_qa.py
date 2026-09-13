"""QA guardrails for the Researcher feasibility review hook."""

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
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


def _expert(key: str, scripts: list[str]) -> StubExpert:
    return StubExpert(BY_KEY[key], scripts)


@pytest.fixture(autouse=True)
def no_adr_commit(monkeypatch):
    monkeypatch.setattr(config, "ADR_ENABLED", False)


async def test_architecture_decision_without_researcher_has_no_feasibility_side_effects(tmp_path):
    seen: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        seen.append(ev)

    engineer = _expert("engineer", ["engineer view"])
    senior = _expert("senior", ["senior view"])
    architect = _expert("architect", ["candidate design", "final design"])
    session = StudioSession(
        "t",
        broadcast,
        experts={"engineer": engineer, "senior": senior, "architect": architect},
        cwd=tmp_path,
    )

    decision = await session._architecture_decision(
        architect, engineer, senior, "build the thing", "research notes"
    )

    assert decision == "final design"
    assert session._feasibility_notes == ""
    assert engineer.calls == 1
    assert senior.calls == 1
    assert architect.calls == 2
    downstream_prompts = [engineer.prompts[0], senior.prompts[0], architect.prompts[1]]
    assert all("研究員可行性評估供參考" not in prompt for prompt in downstream_prompts)
    assert [ev.payload.get("phase") for ev in seen if ev.type == events.EventType.PHASE_CHANGE] == [
        "架構決策"
    ]


async def test_blank_researcher_review_does_not_pollute_downstream_prompts(tmp_path):
    async def broadcast(ev: events.StudioEvent) -> None:
        pass

    engineer = _expert("engineer", ["engineer view"])
    senior = _expert("senior", ["senior view"])
    architect = _expert("architect", ["candidate design", "final design"])
    researcher = _expert("researcher", [""])
    session = StudioSession(
        "t",
        broadcast,
        experts={
            "engineer": engineer,
            "senior": senior,
            "architect": architect,
            "researcher": researcher,
        },
        cwd=tmp_path,
    )

    await session._architecture_decision(
        architect, engineer, senior, "build the thing", "research notes"
    )

    assert researcher.calls == 1
    assert session._feasibility_notes == ""
    downstream_prompts = [engineer.prompts[0], senior.prompts[0], architect.prompts[1]]
    assert all("研究員可行性評估供參考" not in prompt for prompt in downstream_prompts)


async def test_later_decision_without_researcher_does_not_reuse_stale_feasibility(tmp_path):
    async def broadcast(ev: events.StudioEvent) -> None:
        pass

    engineer = _expert("engineer", ["engineer view 1"])
    senior = _expert("senior", ["senior view 1"])
    architect = _expert("architect", ["candidate 1", "final 1"])
    researcher = _expert("researcher", ["重點: first review\n建議: keep it simple"])
    session = StudioSession(
        "t",
        broadcast,
        experts={
            "engineer": engineer,
            "senior": senior,
            "architect": architect,
            "researcher": researcher,
        },
        cwd=tmp_path,
    )
    await session._architecture_decision(
        architect, engineer, senior, "first task", "first research"
    )
    assert session._feasibility_notes == "重點: first review\n建議: keep it simple"

    engineer2 = _expert("engineer", ["engineer view 2"])
    senior2 = _expert("senior", ["senior view 2"])
    architect2 = _expert("architect", ["candidate 2", "final 2"])
    session._experts = {"engineer": engineer2, "senior": senior2, "architect": architect2}

    await session._architecture_decision(
        architect2, engineer2, senior2, "second task", "second research"
    )

    assert session._feasibility_notes == ""
    downstream_prompts = [engineer2.prompts[0], senior2.prompts[0], architect2.prompts[1]]
    assert all("first review" not in prompt for prompt in downstream_prompts)
    assert all("研究員可行性評估供參考" not in prompt for prompt in downstream_prompts)
