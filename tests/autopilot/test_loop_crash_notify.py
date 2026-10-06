"""Autopilot 主迴圈頂層 crash 必須主動通知。"""

from __future__ import annotations

import asyncio as real_asyncio
import types

import pytest

from studio import autopilot, config


@pytest.mark.asyncio
async def test_main_loop_crash_notifies_and_reraises(monkeypatch, tmp_path):
    sent: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "pause.flag")
    monkeypatch.setattr(autopilot, "_self_sig", lambda: 1.0)
    monkeypatch.setattr(autopilot, "_requeue_sideline_task", lambda _reason: None)
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a[0], a[1], k)))
    monkeypatch.setattr(
        autopilot,
        "asyncio",
        types.SimpleNamespace(sleep=real_asyncio.sleep, to_thread=real_asyncio.to_thread),
    )

    async def crash(_startup_sig):
        raise RuntimeError("boom")

    monkeypatch.setattr(autopilot, "_main_loop", crash)

    with pytest.raises(RuntimeError, match="boom"):
        await autopilot.main()

    assert sent == [
        (
            "loop_crash",
            "Autopilot 主迴圈例外退出",
            {"exception_type": "RuntimeError", "error": "boom"},
        )
    ]


@pytest.mark.asyncio
async def test_loop_crash_notification_failure_does_not_mask_original(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "pause.flag")
    monkeypatch.setattr(autopilot, "_self_sig", lambda: 1.0)
    monkeypatch.setattr(autopilot, "_requeue_sideline_task", lambda _reason: None)
    monkeypatch.setattr(
        autopilot,
        "asyncio",
        types.SimpleNamespace(sleep=real_asyncio.sleep, to_thread=real_asyncio.to_thread),
    )

    async def crash(_startup_sig):
        raise RuntimeError("original")

    def notify_crash(*_args, **_kwargs):
        raise AssertionError("notify failed")

    monkeypatch.setattr(autopilot, "_main_loop", crash)
    monkeypatch.setattr(autopilot.notify, "send_bg", notify_crash)

    with pytest.raises(RuntimeError, match="original"):
        await autopilot.main()
