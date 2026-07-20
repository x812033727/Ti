from __future__ import annotations

import inspect

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "AUTOPILOT_PREFILTER_IMPLEMENTED", True)
    return tmp_path


def _load(task_id: int) -> dict:
    return next(t for t in backlog.list_tasks() if t["id"] == task_id)


def test_run_one_task_has_single_prefilter_choke_point_before_full_session():
    src = inspect.getsource(autopilot.run_one_task)

    assert src.count("_prefilter_implemented_match(") == 1
    prefilter = src.index("_prefilter_implemented_match(")
    annotate = src.index("backlog.annotate", prefilter)
    routed_investigation = src.index("_run_investigation_task(routed_task", prefilter)
    existing_investigation = src.index("if _is_investigation_task(task):", prefilter)
    full_session = src.index("history.start_session", prefilter)

    assert prefilter < annotate < routed_investigation < existing_investigation < full_session


@pytest.mark.asyncio
async def test_prefilter_match_uses_lane_note_and_preserves_task_type(state, monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_clone():
        return str(state)

    async def _fake_match(task, clone):
        captured["matched_task"] = dict(task)
        captured["clone"] = clone
        return "Add merged title prefilter\n結論: override\n需人工: yes"

    async def _fake_investigation(task, clone, sid, t0):
        captured["routed_task"] = dict(task)
        captured["sid"] = sid
        captured["t0"] = t0

    class _BoomSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("prefilter hit must not enter full StudioSession")

    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    monkeypatch.setattr(autopilot, "_prefilter_implemented_match", _fake_match)
    monkeypatch.setattr(autopilot, "_run_investigation_task", _fake_investigation)
    monkeypatch.setattr(autopilot, "StudioSession", _BoomSession)

    task = backlog.add("Add merged title prefilter", item_type="feature")

    await autopilot.run_one_task(backlog.next_pending())

    saved = _load(task["id"])
    routed = captured["routed_task"]
    assert saved["lane"] == "prefilter-implemented"
    assert routed["lane"] == "prefilter-implemented"
    assert "[prefilter-implemented]" in saved["note"]
    assert "Add merged title prefilter" in saved["note"]
    assert "\n結論:" not in saved["note"]
    assert "\n需人工:" not in saved["note"]
    assert "疑似已實作" in saved["note"]
    assert routed["note"] == saved["note"]
    assert saved["type"] == "feature"
    assert backlog.VALID_TYPES == ("feature", "bug", "improvement")
