"""QA：每日 PR budget 只能在 _commit_push_merge 真合併成功後消耗。"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config, publisher

_TASK = {"id": "budget-qa", "title": "budget hook", "detail": "verify merge hook"}
_REPO = "owner/ti"


class RunSpy:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "rev-list --count" in joined:
            return (0, "1")
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{_REPO}.git")
        if "pr view" in joined:
            return (0, "42")
        return (0, "")


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _REPO)
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expect_budget_calls"),
    [
        (publisher.MergeOutcome.MERGED, 1),
        (publisher.MergeOutcome.BLOCKED, 0),
    ],
)
async def test_daily_pr_budget_consumed_only_after_real_merge(
    monkeypatch, outcome, expect_budget_calls
):
    budget_calls: list[None] = []

    async def _merge_flow(number, payload, **kwargs):
        return (outcome, "merge detail")

    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(autopilot, "_run", RunSpy())
    monkeypatch.setattr(publisher, "_merge_flow", _merge_flow)
    monkeypatch.setattr(autopilot, "_check_daily_pr_budget", lambda: budget_calls.append(None))

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert len(budget_calls) == expect_budget_calls
    assert ok is (outcome is publisher.MergeOutcome.MERGED), msg


@pytest.mark.asyncio
async def test_dryrun_success_does_not_consume_daily_pr_budget(monkeypatch):
    budget_calls: list[None] = []

    async def _merge_flow(number, payload, **kwargs):
        pytest.fail("dryrun must return before merge flow")

    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    monkeypatch.setattr(autopilot, "_run", RunSpy())
    monkeypatch.setattr(publisher, "_merge_flow", _merge_flow)
    monkeypatch.setattr(autopilot, "_check_daily_pr_budget", lambda: budget_calls.append(None))

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert budget_calls == []
