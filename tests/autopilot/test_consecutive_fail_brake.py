"""Autopilot 主迴圈連續失敗 SLO 煞車。"""

from __future__ import annotations

import asyncio as real_asyncio
import types

import pytest

from studio import autopilot, config


class StopLoop(BaseException):
    """跳出 main() 無限迴圈用；繼承 BaseException 以免被 except Exception 吃掉。"""


def _stub_main_loop(monkeypatch, tmp_path, sleep_fn):
    """主迴圈測試共用 stub（對齊 tests/autopilot/test_quota_gate.py 慣例）：

    - autopilot.asyncio 換成 SimpleNamespace——main() 起不了背景監督 task（缺
      create_task 即靜默跳過），sleep 由測試控制跳出時機，不污染全域 asyncio。
    - STATE_DIR/PAUSE_FILE 指到 tmp、關額度閘門，迴圈前置的 _maybe_* 步驟全部
      因狀態檔不存在而自然 no-op，不打外網。
    """
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "pause.flag")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    monkeypatch.setattr(config, "AUTOPILOT_COOLDOWN", 0)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(autopilot, "_self_sig", lambda: 1.0)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    monkeypatch.setattr(
        autopilot,
        "asyncio",
        types.SimpleNamespace(sleep=sleep_fn, to_thread=real_asyncio.to_thread),
    )


@pytest.fixture(autouse=True)
def _reset_brake(monkeypatch):
    monkeypatch.setattr(autopilot, "_consecutive_fail_count", 0)
    monkeypatch.setattr(autopilot, "_consecutive_fail_notified", False)
    monkeypatch.setattr(autopilot, "_consecutive_fail_pause_active", False)


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

    _stub_main_loop(monkeypatch, tmp_path, stop_after_second_sleep)
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 2)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: next(tasks))
    monkeypatch.setattr(autopilot.backlog, "get", lambda task_id: {"status": statuses[task_id]})
    monkeypatch.setattr(autopilot, "run_one_task", fail_task)
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a[0], a[1], k)))

    with pytest.raises(StopLoop):
        await autopilot.main()

    assert config.autopilot_paused() is True
    assert len(sent) == 1
    assert sent[0][0] == "consecutive_fail_pause"
    assert sent[0][2]["consecutive_fail_count"] == 2


@pytest.mark.asyncio
async def test_main_pending_after_task_does_not_count_or_reset(monkeypatch, tmp_path):
    processed: list[int] = []
    statuses: dict[int, str] = {}
    tasks = iter([{"id": 1, "title": "provider unavailable"}])

    async def pending_task(task):
        processed.append(task["id"])
        statuses[task["id"]] = "pending"

    async def stop_after_first_sleep(_delay):
        if processed:
            raise StopLoop

    _stub_main_loop(monkeypatch, tmp_path, stop_after_first_sleep)
    monkeypatch.setattr(autopilot, "_consecutive_fail_count", 1)
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 2)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: next(tasks))
    monkeypatch.setattr(autopilot.backlog, "get", lambda task_id: {"status": statuses[task_id]})
    monkeypatch.setattr(autopilot, "run_one_task", pending_task)
    monkeypatch.setattr(autopilot, "_pause", lambda _reason: pytest.fail("pending must not pause"))
    monkeypatch.setattr(
        autopilot.notify,
        "send_bg",
        lambda *a, **k: pytest.fail("pending must not notify"),
    )

    with pytest.raises(StopLoop):
        await autopilot.main()

    assert autopilot._consecutive_fail_count == 1
    assert config.autopilot_paused() is False


@pytest.mark.asyncio
async def test_manual_pause_recovery_starts_new_notification_period(monkeypatch, tmp_path):
    processed: list[int] = []
    sent: list[tuple[str, str, dict]] = []
    statuses: dict[int, str] = {}
    tasks = iter(
        [
            {"id": 1, "title": "fail 1"},
            {"id": 2, "title": "fail 2"},
            {"id": 3, "title": "fail 3"},
            {"id": 4, "title": "fail 4"},
        ]
    )

    async def fail_task(task):
        processed.append(task["id"])
        statuses[task["id"]] = "failed"

    async def remove_pause_then_stop(_delay):
        if config.autopilot_paused():
            config.AUTOPILOT_PAUSE_FILE.unlink()
        if len(sent) >= 2:
            raise StopLoop

    _stub_main_loop(monkeypatch, tmp_path, remove_pause_then_stop)
    monkeypatch.setattr(config, "AUTOPILOT_CONSECUTIVE_FAIL_PAUSE", 2)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: next(tasks))
    monkeypatch.setattr(autopilot.backlog, "get", lambda task_id: {"status": statuses[task_id]})
    monkeypatch.setattr(autopilot, "run_one_task", fail_task)
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a[0], a[1], k)))

    with pytest.raises(StopLoop):
        await autopilot.main()

    assert processed == [1, 2, 3, 4]
    assert len(sent) == 2
    assert sent[0][0] == "consecutive_fail_pause"
    assert sent[1][0] == "consecutive_fail_pause"


@pytest.mark.asyncio
async def test_non_slo_pause_recovery_does_not_reset_failures(monkeypatch, tmp_path):
    async def clear_pause(_delay):
        if config.AUTOPILOT_PAUSE_FILE.exists():
            config.AUTOPILOT_PAUSE_FILE.unlink()

    def stop_after_unpause():
        # 暫停期間（_pause_tick 首輪也會呼叫本函式且包在 suppress 裡）不停；
        # pause 檔被移除、迴圈真正恢復取任務後才跳出。
        if not config.AUTOPILOT_PAUSE_FILE.exists():
            raise StopLoop

    _stub_main_loop(monkeypatch, tmp_path, clear_pause)
    monkeypatch.setattr(autopilot, "_consecutive_fail_count", 1)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", stop_after_unpause)
    config.AUTOPILOT_PAUSE_FILE.write_text("provider unavailable\n", encoding="utf-8")

    with pytest.raises(StopLoop):
        await autopilot.main()

    assert autopilot._consecutive_fail_count == 1


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
