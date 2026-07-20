from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", True)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_REFUTE", False)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_TIMEOUT", 30)
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(config, "AUTOPILOT_PREFILTER_IMPLEMENTED", True)
    monkeypatch.setattr(config, "AUTOPILOT_PREFILTER_RATIO", 0.80)
    autopilot._MERGED_TITLE_CACHE.clear()
    return tmp_path


def _load(task_id: int) -> dict:
    return next(t for t in backlog.list_tasks() if t["id"] == task_id)


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    class _FakeExpert:
        last_prompt = ""

        def __init__(self, *args, **kwargs):
            pass

        async def speak(self, prompt, on_event):
            type(self).last_prompt = prompt
            return reply

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)
    return _FakeExpert


@pytest.mark.asyncio
async def test_run_one_task_prefilter_routes_to_investigation_with_note(state, monkeypatch):
    async def _fake_clone():
        return str(state)

    async def _fake_corpus(clone, repo=None):
        return ["Add merged title prefilter"]

    class _BoomSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("prefilter 命中時不得建多專家 session")

    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    monkeypatch.setattr(autopilot, "_recent_merged_title_corpus", _fake_corpus)
    monkeypatch.setattr(autopilot, "StudioSession", _BoomSession)
    fake_expert = _patch_expert(monkeypatch, "結論: 已確認近期 merged PR 已涵蓋\n證據: git log:1\n")

    task = backlog.add("Add merged title prefilter")
    await autopilot.run_one_task(backlog.next_pending())

    saved = _load(task["id"])
    assert saved["status"] == "done"
    assert saved["lane"] == "prefilter-implemented"
    assert "Add merged title prefilter" in saved["note"]
    assert "[調查結論]" in saved["note"]
    assert "任務備註：" in fake_expert.last_prompt


@pytest.mark.asyncio
async def test_prefilter_skips_full_lane_before_fetch(state, monkeypatch):
    async def _boom_corpus(clone, repo=None):
        raise AssertionError("lane=full 不應取 merged title 語料")

    monkeypatch.setattr(autopilot, "_recent_merged_title_corpus", _boom_corpus)

    assert (
        await autopilot._prefilter_implemented_match(
            {"title": "Add merged title prefilter", "lane": "full"},
            str(state),
        )
        is None
    )
