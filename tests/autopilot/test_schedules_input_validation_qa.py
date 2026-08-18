"""QA coverage for schedule input validation hardening."""

from __future__ import annotations

import calendar

import pytest

from studio import admission_mode, backlog, config, schedules


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "ap"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state_dir)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    admission_mode.bootstrap_at_task_boundary(
        config.TASK_ADMISSION_MODE,
        initial_effective=config.TASK_ADMISSION_MODE,
        release_holds=lambda _mode: 0,
    )


def _utc(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


@pytest.mark.parametrize(
    "recurrence",
    [
        {"kind": "daily", "time": "8:00"},
        {"kind": "daily", "time": "08:0"},
        {"kind": "weekly", "time": "8:05", "weekday": 0},
        {"kind": "weekly", "time": "08:5", "weekday": 0},
    ],
)
def test_schedule_time_requires_zero_padded_hh_mm(recurrence):
    assert schedules.validate_recurrence(recurrence) == "time 須為 HH:MM(UTC)"
    assert schedules.create("bad time", "", recurrence)[0] is None


def test_enqueue_due_normalizes_corrupt_persisted_priority():
    sched, err = schedules.create(
        "壞 priority 仍須入列",
        "",
        {"kind": "daily", "time": "08:00"},
        priority=0,
    )
    assert err == ""

    data = schedules._load()
    data["schedules"][0]["priority"] = "not-an-int"
    schedules._save(data)

    assert schedules.enqueue_due(_utc(2026, 7, 20, 8, 1)) == 1
    [task] = backlog.list_tasks()
    assert task["title"] == "[排程] 壞 priority 仍須入列"
    assert task["priority"] == 1
    assert schedules.list_schedules()[0]["last_fired_key"] == "d-20260720"
