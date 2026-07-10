"""`_wrap_up` 裁決原因回寫（完成率第三輪修法一的 (a)-lite 配套）。

PM 驗收判「未完成」時輸出 `原因: <一句根因>`，經 run() 回傳 dict 的 `incomplete_reason`
傳到 autopilot 的失敗 note——讓「討論未達完成」從無資訊量的一句話變成帶根因的分診依據。
PM 沒給原因時以客觀狀態（demo_veto/all_ok/critic）合成兜底，不回報空白。

範式沿用 tests/core/test_orchestrator.py（StubExpert + collect + monkeypatch _commit）。
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


def _session(monkeypatch):
    _, bc = collect()
    session = StudioSession("t", bc, experts={}, cwd=None)

    async def _noop_commit(*args, **kwargs):
        pass

    monkeypatch.setattr(session, "_commit", _noop_commit)
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    return session


@pytest.mark.asyncio
async def test_pm_reason_captured_on_incomplete(monkeypatch):
    session = _session(monkeypatch)
    pm = StubExpert(BY_KEY["pm"], ["決議: 未完成\n原因: QA 無法存取 $TMPDIR 證據檔", "檢討 ok"])

    done = await session._wrap_up(pm, all_ok=False)

    assert not done
    assert session._incomplete_reason == "QA 無法存取 $TMPDIR 證據檔"


@pytest.mark.asyncio
async def test_missing_reason_synthesized_from_objective_state(monkeypatch):
    session = _session(monkeypatch)
    # PM 只說未完成、沒給原因 → 以客觀狀態兜底（demo_veto 優先）
    pm = StubExpert(BY_KEY["pm"], ["決議: 未完成", "檢討 ok"])

    done = await session._wrap_up(pm, all_ok=False, demo_veto=True)

    assert not done
    assert "Demo" in session._incomplete_reason

    session2 = _session(monkeypatch)
    pm2 = StubExpert(BY_KEY["pm"], ["決議: 未完成", "檢討 ok"])
    await session2._wrap_up(pm2, all_ok=False)
    assert "三審" in session2._incomplete_reason


@pytest.mark.asyncio
async def test_completed_leaves_reason_empty(monkeypatch):
    session = _session(monkeypatch)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    pm = StubExpert(BY_KEY["pm"], ["決議: 完成", "檢討 ok"])

    done = await session._wrap_up(pm, all_ok=True)

    assert done
    assert session._incomplete_reason == ""
