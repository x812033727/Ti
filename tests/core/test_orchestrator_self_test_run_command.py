from __future__ import annotations

import pytest

from studio import events, runner
from studio.orchestrator import LaneContext, StudioSession
from studio.runner import RunOutput


async def _broadcast(_event: events.StudioEvent) -> None:
    return None


class RunCommandSpy:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def __call__(self, cwd, command, timeout=None, sandbox=None) -> RunOutput:
        self.commands.append(command)
        return RunOutput(command=command, exit_code=0, output="ok", timed_out=False)


@pytest.mark.asyncio
async def test_self_test_updates_run_command_from_engineer_report(monkeypatch, tmp_path):
    spy = RunCommandSpy()
    monkeypatch.setattr(runner, "run_command", spy)
    session = StudioSession("t", _broadcast, cwd=tmp_path)
    session._run_command = "old-command"
    ctx = LaneContext("main", tmp_path, {})

    await session._self_test(ctx, "done\n執行指令: new-command")

    assert session._run_command == "new-command"
    assert spy.commands == ["new-command"]


@pytest.mark.asyncio
async def test_self_test_keeps_existing_run_command_without_engineer_report(
    monkeypatch, tmp_path
):
    spy = RunCommandSpy()
    monkeypatch.setattr(runner, "run_command", spy)
    session = StudioSession("t", _broadcast, cwd=tmp_path)
    session._run_command = "old-command"
    ctx = LaneContext("main", tmp_path, {})

    await session._self_test(ctx, "done without run command")

    assert session._run_command == "old-command"
    assert spy.commands == ["old-command"]
