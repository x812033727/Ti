"""Autopilot subprocess helper 取消收尾。

契約：`_run()` 若被外層 coroutine 取消，必須清掉 subprocess group，並保留
CancelledError 向上傳播，讓呼叫端仍能照原取消流程停機。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot


@pytest.mark.asyncio
async def test_run_cancel_kills_process_group_and_reraises(monkeypatch):
    started = asyncio.Event()

    class FakeProc:
        returncode = None

        def __init__(self):
            self.waited = False

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        async def wait(self):
            self.waited = True
            return -9

    proc = FakeProc()
    killed = []

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(autopilot.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(autopilot.runner, "kill_process_group", lambda p: killed.append(p))

    task = asyncio.create_task(autopilot._run(["sleep", "999"], timeout=60))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert killed == [proc]
    assert proc.waited is True
