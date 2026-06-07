"""backlog 持久任務佇列的單元測試（不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    return tmp_path


def test_add_and_counts(state):
    t = backlog.add("任務 A")
    assert t and t["status"] == "pending" and t["id"] == 1
    backlog.add_many(["任務 B", "任務 C"])
    assert backlog.counts()["pending"] == 3


def test_dedup_pending(state):
    backlog.add("重複任務")
    assert backlog.add("重複任務") is None  # 仍 pending → 視為重複
    assert backlog.counts()["pending"] == 1


def test_empty_title_rejected(state):
    assert backlog.add("   ") is None


def test_next_pending_is_oldest(state):
    a = backlog.add("先")
    backlog.add("後")
    assert backlog.next_pending()["id"] == a["id"]


def test_status_transitions(state):
    t = backlog.add("做這個")
    backlog.set_status(t["id"], "in_progress", session_id="s1")
    cur = backlog.list_tasks("in_progress")[0]
    assert cur["attempts"] == 1 and cur["session_id"] == "s1"
    backlog.set_status(t["id"], "done")
    assert backlog.counts()["done"] == 1
    assert backlog.next_pending() is None


def test_invalid_status_raises(state):
    t = backlog.add("x")
    with pytest.raises(ValueError):
        backlog.set_status(t["id"], "bogus")


def test_pause_switch(tmp_path, monkeypatch):
    pf = tmp_path / "PAUSED"
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", pf)
    monkeypatch.delenv("TI_AUTOPILOT_PAUSED", raising=False)
    assert config.autopilot_paused() is False
    pf.write_text("x")
    assert config.autopilot_paused() is True
