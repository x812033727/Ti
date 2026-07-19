"""Autopilot 主迴圈連續失敗 SLO 煞車。"""

from __future__ import annotations

import pytest

from studio import autopilot, config


@pytest.fixture(autouse=True)
def _reset_brake(monkeypatch):
    monkeypatch.setattr(autopilot, "_consecutive_fail_count", 0)
    monkeypatch.setattr(autopilot, "_consecutive_fail_notified", False)


def _drive_statuses(monkeypatch, statuses):
    pending = iter(statuses)
    monkeypatch.setattr(autopilot.backlog, "get", lambda _task_id: {"status": next(pending)})
    for idx, _status in enumerate(statuses, start=1):
        autopilot._record_consecutive_fail_outcome(idx)


def _capture_pause(pauses):
    def pause(reason):
        pauses.append(reason)
        return True

    return pause


def _capture_failed_pause(pauses):
    def pause(reason):
        pauses.append(reason)
        return False

    return pause


@pytest.mark.asyncio
async def test_main_pauses_and_notifies_once_after_consecutive_failures(monkeypatch, tmp_path):
    class StopLoop(Exception):
        pass

    processed: list[int] = []
    sent: list[tuple[str, str, dict]] = []
    statuses: dict[int, str] = {}
    tasks = iter([{"id": 1, "title": "fail 1"}, {"id": 2, "title": "fail 2"}])

    async def fail_task(task):
        processed.append(task["id"])
        statuses[task["id"]] = "failed"

    async def stop_after_second_sleep(_delay):
        if len(processed) >= 2:
            raise StopLoop

    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 2)
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "pause.flag")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    monkeypatch.setattr(config, "AUTOPILOT_COOLDOWN", 0)
    monkeypatch.setattr(autopilot, "_self_sig", lambda: 1.0)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: next(tasks))
    monkeypatch.setattr(autopilot.backlog, "get", lambda task_id: {"status": statuses[task_id]})
    monkeypatch.setattr(autopilot, "run_one_task", fail_task)
    monkeypatch.setattr(autopilot.asyncio, "sleep", stop_after_second_sleep)
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a[0], a[1], k)))

    with pytest.raises(StopLoop):
        await autopilot.main()

    assert config.autopilot_paused() is True
    assert len(sent) == 1
    assert sent[0][0] == "consecutive_fail_pause"
    assert sent[0][2]["consecutive_fail_count"] == 2


def test_unreached_threshold_does_not_pause(monkeypatch):
    pauses: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 3)
    monkeypatch.setattr(autopilot, "_pause", _capture_pause(pauses))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a, k)))

    _drive_statuses(monkeypatch, ["failed", "failed"])

    assert pauses == []
    assert sent == []


def test_done_resets_consecutive_failures(monkeypatch):
    pauses: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 5)
    monkeypatch.setattr(autopilot, "_pause", _capture_pause(pauses))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a, k)))

    _drive_statuses(monkeypatch, ["failed", "failed", "failed", "failed", "done", "failed"])

    assert pauses == []
    assert sent == []


def test_done_resets_notification_period(monkeypatch):
    pauses: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 2)
    monkeypatch.setattr(autopilot, "_pause", _capture_pause(pauses))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a, k)))

    _drive_statuses(monkeypatch, ["failed", "failed", "failed", "done", "failed", "failed"])

    assert len(pauses) == 2
    assert len(sent) == 2


def test_pending_does_not_count_or_reset(monkeypatch):
    pauses: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 2)
    monkeypatch.setattr(autopilot, "_pause", _capture_pause(pauses))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a, k)))

    _drive_statuses(monkeypatch, ["failed", "pending", "failed", "failed"])

    assert len(pauses) == 1
    assert len(sent) == 1
    assert autopilot._consecutive_fail_count == 3


def test_pause_returns_false_on_write_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "missing" / "pause.flag")

    assert autopilot._pause("write failure") is False
    assert config.autopilot_paused() is False


def test_pause_failure_does_not_latch_notification_period(monkeypatch):
    pause_attempts: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 2)
    monkeypatch.setattr(autopilot, "_pause", _capture_failed_pause(pause_attempts))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a, k)))

    _drive_statuses(monkeypatch, ["failed", "failed", "failed"])

    assert len(pause_attempts) == 2
    assert sent == []
    assert autopilot._consecutive_fail_notified is False


def test_disabled_consecutive_fail_pause_has_zero_side_effects(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 0)
    monkeypatch.setattr(
        autopilot.backlog,
        "get",
        lambda _task_id: pytest.fail("disabled brake must not read backlog"),
    )
    monkeypatch.setattr(autopilot, "_pause", lambda _reason: pytest.fail("must not pause"))
    monkeypatch.setattr(
        autopilot.notify,
        "send_bg",
        lambda *a, **k: pytest.fail("must not notify"),
    )

    autopilot._record_consecutive_fail_outcome(1)


def test_config_reload_reads_consecutive_fail_pause(monkeypatch):
    snapshot = {name: getattr(config, name) for name in dir(config) if name.isupper()}
    try:
        monkeypatch.setenv("TI_AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", "7")
        config.reload()
        assert config.AUTOPILOT_CONSECUTIVE_FAIL_PAUSE == 7
    finally:
        for name, value in snapshot.items():
            setattr(config, name, value)
