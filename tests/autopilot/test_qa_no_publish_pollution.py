"""QA guard: autopilot must never publish core changes through the project repo."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from studio import autopilot, config, publisher

_TASK = {"id": "91", "title": "repo guard", "detail": "keep core changes in autopilot repo"}
_AUTOPILOT_REPO = "core/autopilot"
_PROJECT_REPO = "product/project"


class RunSpy:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "rev-list --count" in joined:
            return (0, "1")
        if "pr view" in joined:
            return (0, "42")
        return (0, "")

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def called(self, fragment: str) -> bool:
        return any(fragment in j for j in self.joined())


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _AUTOPILOT_REPO)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)
    monkeypatch.setattr(config, "PUBLISH_REPO", _PROJECT_REPO)


@pytest.mark.asyncio
async def test_merge_flow_observes_autopilot_repo_override(monkeypatch):
    spy = RunSpy()
    observed_repos: list[str] = []

    async def _merge_flow(number, payload, **kwargs):
        observed_repos.append(publisher.current_repo())
        return (publisher.MergeOutcome.MERGED, "merged")

    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert observed_repos == [_AUTOPILOT_REPO]
    assert publisher.current_repo() == _PROJECT_REPO
    assert spy.called(f"pr create -R {_AUTOPILOT_REPO}")
    assert not spy.called(f"pr create -R {_PROJECT_REPO}")


@pytest.mark.asyncio
async def test_empty_autopilot_repo_aborts_before_push_or_pr(monkeypatch):
    run = AsyncMock()
    merge_flow = AsyncMock()
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "")
    monkeypatch.setattr(autopilot, "_run", run)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "AUTOPILOT_REPO" in msg
    run.assert_not_awaited()
    merge_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_repo_same_as_autopilot_repo_aborts_without_side_effects(monkeypatch):
    run = AsyncMock()
    merge_flow = AsyncMock()
    monkeypatch.setattr(config, "PUBLISH_REPO", _AUTOPILOT_REPO)
    monkeypatch.setattr(autopilot, "_run", run)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "PUBLISH_REPO" in msg
    run.assert_not_awaited()
    merge_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_publish_repo_does_not_block_autopilot_push(monkeypatch):
    spy = RunSpy()

    async def _merge_flow(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "merged")

    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert spy.called(f"pr create -R {_AUTOPILOT_REPO}")
    assert any("push" in call for call in spy.calls)
