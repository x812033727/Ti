"""Autopilot provider unavailable handling."""

from __future__ import annotations

import pytest

from studio import autopilot


@pytest.mark.asyncio
async def test_run_one_task_pauses_on_provider_unavailable(monkeypatch, tmp_path):
    """Provider 額度/可用性問題應暫停 autopilot，避免把任務打成 failed 後繼續燒。"""
    clone = tmp_path / "clone"
    clone.mkdir()
    statuses = []
    pauses = []

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _requirement):
            return {
                "completed": False,
                "followups": [],
                "followup_items": [],
                "core_changes": [],
                "provider_unavailable": "codex",
            }

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", FakeSession)
    monkeypatch.setattr(autopilot.history, "start_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autopilot.history, "record_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autopilot.history, "finish_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        autopilot.backlog,
        "set_status",
        lambda task_id, status, **kw: statuses.append((task_id, status, kw)),
    )
    monkeypatch.setattr(autopilot.backlog, "add_items", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(autopilot.backlog, "add_many", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(autopilot, "_pause", lambda reason: pauses.append(reason))

    await autopilot.run_one_task({"id": 7, "title": "觸發 Codex usage limit"})

    assert statuses[0][0:2] == (7, "in_progress")
    assert statuses[0][2]["session_id"].startswith("ap")
    assert statuses[-1] == (7, "pending", {"note": "codex provider unavailable"})
    assert pauses == ["codex provider unavailable"]
