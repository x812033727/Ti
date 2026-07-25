"""Task-admission incident 生命週期與持久化契約。"""

from __future__ import annotations

import importlib
import json
import math
import multiprocessing
import queue
import threading
import time
from pathlib import Path

import pytest

from studio import admission_incidents


def _concurrent_observe_process(
    state_dir: str,
    start,
    delivered,
    completed,
) -> None:
    """獨立 process 模擬兩個 worker 同時重送同一 durable outbox event。"""
    start.wait(timeout=5)

    def notify(event):
        delivered.put(event.event_id)
        time.sleep(0.1)

    receipt = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=notify,
        state_dir=Path(state_dir),
    )
    completed.put(receipt.notification)


def test_first_fault_opens_persistent_incident_and_pages(tmp_path: Path):
    events = []

    receipt = admission_incidents.observe(
        admission_incidents.Faulted(
            error_code="invalid_json",
            effective_mode="enforce",
            effective_generation=7,
        ),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert receipt.phase == "open"
    assert receipt.durability == "durable"
    assert receipt.notification == "queued"
    assert len(events) == 1
    event = events[0]
    assert event.kind == "admission_mode_fault"
    assert event.payload == {
        "event_id": event.event_id,
        "incident_id": receipt.incident_id,
        "error_code": "invalid_json",
        "effective_mode": "enforce",
        "effective_generation": 7,
    }

    current = admission_incidents.snapshot(state_dir=tmp_path)
    assert current["active"] is True
    assert current["incident_id"] == receipt.incident_id
    assert current["error_code"] == "invalid_json"
    assert current["first_seen_at"] == current["last_seen_at"]
    assert current["last_effective_mode"] == "enforce"
    assert current["last_effective_generation"] == 7
    assert current["durability"] == "durable"


def test_same_fault_dedupes_across_restart_but_code_change_pages_again(tmp_path: Path):
    events = []
    first = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 7),
        notify=events.append,
        state_dir=tmp_path,
    )

    repeated = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 7),
        notify=events.append,
        state_dir=tmp_path,
    )
    importlib.reload(admission_incidents)
    after_restart = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 7),
        notify=events.append,
        state_dir=tmp_path,
    )
    changed = admission_incidents.observe(
        admission_incidents.Faulted("state_unreadable", "enforce", 7),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert (
        repeated.incident_id
        == after_restart.incident_id
        == changed.incident_id
        == first.incident_id
    )
    assert repeated.notification == after_restart.notification == "deduped"
    assert changed.notification == "queued"
    assert [event.kind for event in events] == [
        "admission_mode_fault",
        "admission_mode_fault",
    ]
    assert [event.payload["error_code"] for event in events] == [
        "invalid_json",
        "state_unreadable",
    ]
    assert events[0].event_id != events[1].event_id


def test_waiting_does_not_recover_and_restored_pages_once(tmp_path: Path):
    events = []
    opened = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 3),
        notify=events.append,
        state_dir=tmp_path,
    )

    waiting = admission_incidents.observe(
        admission_incidents.Waiting(),
        notify=events.append,
        state_dir=tmp_path,
    )
    assert waiting.phase == "open"
    assert admission_incidents.snapshot(state_dir=tmp_path)["active"] is True
    assert len(events) == 1

    recovered = admission_incidents.observe(
        admission_incidents.IntakeRestored("enforce", 4),
        notify=events.append,
        state_dir=tmp_path,
    )
    repeated = admission_incidents.observe(
        admission_incidents.IntakeRestored("enforce", 4),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert recovered.incident_id == repeated.incident_id == opened.incident_id
    assert recovered.phase == repeated.phase == "recovered"
    assert recovered.notification == "queued"
    assert repeated.notification == "deduped"
    assert [event.kind for event in events] == [
        "admission_mode_fault",
        "admission_mode_recovered",
    ]
    assert events[1].payload["incident_id"] == opened.incident_id
    assert events[1].payload["effective_mode"] == "enforce"
    assert events[1].payload["effective_generation"] == 4
    assert admission_incidents.snapshot(state_dir=tmp_path)["active"] is False

    next_fault = admission_incidents.observe(
        admission_incidents.Faulted("invalid_schema", "enforce", 4),
        notify=events.append,
        state_dir=tmp_path,
    )
    assert next_fault.incident_id != opened.incident_id
    assert next_fault.notification == "queued"
    assert events[-1].kind == "admission_mode_fault"


def test_ledger_write_failure_pages_and_dedupes_in_process_memory(
    tmp_path: Path,
    monkeypatch,
):
    events = []
    real_write = admission_incidents.secure_write_root

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full: private detail must not escape")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    first = admission_incidents.observe(
        admission_incidents.Faulted("state_write_failed", "shadow", 2),
        notify=events.append,
        state_dir=tmp_path,
    )
    repeated = admission_incidents.observe(
        admission_incidents.Faulted("state_write_failed", "shadow", 2),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert first.durability == repeated.durability == "memory_only"
    assert first.notification == "queued"
    assert repeated.notification == "deduped"
    assert first.diagnostic == repeated.diagnostic == "ledger_write_failed"
    assert len(events) == 1
    assert admission_incidents.snapshot(state_dir=tmp_path)["active"] is True
    assert admission_incidents.snapshot(state_dir=tmp_path)["durability"] == "memory_only"

    monkeypatch.setattr(admission_incidents, "secure_write_root", real_write)
    persisted = admission_incidents.observe(
        admission_incidents.Faulted("state_write_failed", "shadow", 2),
        notify=events.append,
        state_dir=tmp_path,
    )
    assert persisted.durability == "durable"
    assert persisted.notification == "deduped"
    assert len(events) == 1
    assert admission_incidents.snapshot(state_dir=tmp_path)["durability"] == "durable"


def test_notifier_failure_leaves_durable_outbox_for_next_observation(tmp_path: Path):
    def unavailable(_event):
        raise RuntimeError("webhook adapter unavailable")

    deferred = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 8),
        notify=unavailable,
        state_dir=tmp_path,
    )
    importlib.reload(admission_incidents)
    events = []
    retried = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 8),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert deferred.durability == "durable"
    assert deferred.notification == "deferred"
    assert deferred.diagnostic == "notification_failed"
    assert retried.notification == "queued"
    assert len(events) == 1
    assert events[0].kind == "admission_mode_fault"


def test_notifier_rejection_leaves_durable_outbox_for_retry(tmp_path: Path):
    rejected = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 8),
        notify=lambda _event: False,
        state_dir=tmp_path,
    )
    events = []
    retried = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 8),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert rejected.durability == "durable"
    assert rejected.notification == "deferred"
    assert rejected.diagnostic == "notification_rejected"
    assert retried.notification == "queued"
    assert len(events) == 1


def test_pending_closed_incident_events_survive_a_new_incident(tmp_path: Path):
    opened = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: False,
        state_dir=tmp_path,
    )
    admission_incidents.observe(
        admission_incidents.IntakeRestored("shadow", 1),
        notify=lambda _event: False,
        state_dir=tmp_path,
    )
    events = []

    next_fault = admission_incidents.observe(
        admission_incidents.Faulted("invalid_schema", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert next_fault.durability == "durable"
    assert next_fault.incident_id != opened.incident_id
    assert [event.kind for event in events] == [
        "admission_mode_fault",
        "admission_mode_recovered",
        "admission_mode_fault",
    ]
    assert events[0].payload["incident_id"] == events[1].payload["incident_id"]
    assert events[2].payload["incident_id"] == next_fault.incident_id


def test_concurrent_observers_deliver_each_pending_event_once(tmp_path: Path):
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: False,
        state_dir=tmp_path,
    )
    rendezvous = threading.Barrier(2)
    events = []

    def notify(event):
        events.append(event)
        try:
            rendezvous.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass

    threads = [
        threading.Thread(
            target=admission_incidents.observe,
            args=(admission_incidents.Faulted("invalid_json", "shadow", 1),),
            kwargs={"notify": notify, "state_dir": tmp_path},
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(events) == 1


def test_cross_process_observers_serialize_durable_delivery(tmp_path: Path):
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: False,
        state_dir=tmp_path,
    )
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    delivered = context.Queue()
    completed = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_observe_process,
            args=(str(tmp_path), start, delivered, completed),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted(completed.get(timeout=1) for _ in processes) == ["deduped", "queued"]
    delivered_ids = [delivered.get(timeout=1)]
    with pytest.raises(queue.Empty):
        delivered.get(timeout=0.2)
    assert len(set(delivered_ids)) == 1


def test_ack_read_failure_never_replaces_a_newer_durable_transition(
    tmp_path: Path,
    monkeypatch,
):
    events = []
    fail_next_read = False
    real_read = admission_incidents._read_unlocked

    def flaky_read(state_dir):
        nonlocal fail_next_read
        if fail_next_read:
            fail_next_read = False
            raise admission_incidents._LedgerError("ledger_unreadable")
        return real_read(state_dir)

    monkeypatch.setattr(admission_incidents, "_read_unlocked", flaky_read)

    def first_notify(event):
        nonlocal fail_next_read
        events.append(event)
        # 模擬另一個 observer 在網路等待期間提交較新的 code transition。
        with admission_incidents._locked(tmp_path):
            current = real_read(tmp_path)
            admission_incidents._apply_observation(
                current,
                admission_incidents.Faulted("state_unreadable", "shadow", 1),
                now=time.time(),
            )
            admission_incidents._write_unlocked(current, tmp_path)
        fail_next_read = True

    first = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=first_notify,
        state_dir=tmp_path,
    )
    after_failed_ack = admission_incidents.snapshot(state_dir=tmp_path)
    retried = admission_incidents.observe(
        admission_incidents.Faulted("state_unreadable", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert first.notification == "queued"
    assert first.diagnostic == "ledger_unreadable"
    assert after_failed_ack["durability"] == "durable"
    assert after_failed_ack["error_code"] == "state_unreadable"
    assert after_failed_ack["sequence"] == 2
    assert retried.notification == "queued"
    assert [event.payload["error_code"] for event in events] == [
        "invalid_json",
        "state_unreadable",
    ]
    assert admission_incidents.snapshot(state_dir=tmp_path)["error_code"] == "state_unreadable"


def test_accepted_event_is_not_resent_when_next_transition_write_also_fails(
    tmp_path: Path,
    monkeypatch,
):
    events = []
    real_write = admission_incidents.secure_write_root
    writes = 0

    def fail_after_transition(path, body):
        nonlocal writes
        writes += 1
        if writes >= 2:
            raise OSError("disk full")
        return real_write(path, body)

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_after_transition)
    first = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    repeated = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert repeated.incident_id == first.incident_id
    assert first.notification == "queued"
    assert repeated.notification == "deduped"
    assert len(events) == 1


def test_accepted_event_survives_corrupt_ack_and_quarantine_without_repage(
    tmp_path: Path,
):
    events = []
    ledger_path = tmp_path / "admission_incident.json"

    def accept_then_corrupt(event):
        events.append(event)
        ledger_path.write_text("{broken", encoding="utf-8")

    first = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=accept_then_corrupt,
        state_dir=tmp_path,
    )
    repeated = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    ledger_path.rename(tmp_path / "admission_incident.json.quarantine")
    repaired = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert repeated.incident_id == repaired.incident_id == first.incident_id
    assert first.notification == "queued"
    assert repeated.notification == repaired.notification == "deduped"
    assert repaired.durability == "durable"
    assert len(events) == 1


def test_later_drain_event_uses_its_own_snapshot_when_ack_becomes_corrupt(
    tmp_path: Path,
):
    events = []
    ledger_path = tmp_path / "admission_incident.json"

    def notify(event):
        events.append(event)
        if event.payload["error_code"] == "invalid_json":
            with admission_incidents._locked(tmp_path):
                current = admission_incidents._read_unlocked(tmp_path)
                admission_incidents._apply_observation(
                    current,
                    admission_incidents.Faulted("state_unreadable", "shadow", 1),
                    now=time.time(),
                )
                admission_incidents._write_unlocked(current, tmp_path)
        elif len(events) == 2:
            ledger_path.write_text("{broken", encoding="utf-8")

    first = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=notify,
        state_dir=tmp_path,
    )
    repeated = admission_incidents.observe(
        admission_incidents.Faulted("state_unreadable", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert repeated.incident_id == first.incident_id
    assert repeated.notification == "deduped"
    assert [event.payload["error_code"] for event in events] == [
        "invalid_json",
        "state_unreadable",
    ]


def test_memory_fallback_rebases_on_a_concurrent_durable_transition(
    tmp_path: Path,
    monkeypatch,
):
    events = []
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    real_write = admission_incidents.secure_write_root

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    admission_incidents.observe(
        admission_incidents.Faulted("state_unreadable", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    memory = admission_incidents.snapshot(state_dir=tmp_path)
    assert memory["error_code"] == "state_unreadable"
    assert memory["durability"] == "memory_only"

    monkeypatch.setattr(admission_incidents, "secure_write_root", real_write)
    with admission_incidents._locked(tmp_path):
        disk = admission_incidents._read_unlocked(tmp_path)
        admission_incidents._apply_observation(
            disk,
            admission_incidents.Faulted("invalid_schema", "shadow", 1),
            now=time.time(),
        )
        admission_incidents._write_unlocked(disk, tmp_path)

    admission_incidents.observe(
        admission_incidents.Faulted("invalid_schema", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    projection = admission_incidents.snapshot(state_dir=tmp_path)

    assert projection["durability"] == "durable"
    assert projection["error_code"] == "invalid_schema"
    assert projection["sequence"] > memory["sequence"]
    assert events[-1].payload["error_code"] == "invalid_schema"


def test_memory_branch_cannot_resurrect_an_incident_recovered_on_disk(
    tmp_path: Path,
    monkeypatch,
):
    events = []
    opened = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    real_write = admission_incidents.secure_write_root

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )

    monkeypatch.setattr(admission_incidents, "secure_write_root", real_write)
    with admission_incidents._locked(tmp_path):
        disk = admission_incidents._read_unlocked(tmp_path)
        admission_incidents._apply_observation(
            disk,
            admission_incidents.IntakeRestored("shadow", 1),
            now=time.time(),
        )
        disk["outbox"] = []
        admission_incidents._write_unlocked(disk, tmp_path)

    after_recovery = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=events.append,
        state_dir=tmp_path,
    )
    projection = admission_incidents.snapshot(state_dir=tmp_path)

    assert after_recovery.incident_id != opened.incident_id
    assert projection["incident_id"] == after_recovery.incident_id
    assert projection["active"] is True
    assert len(events) == 2
    assert events[0].event_id != events[1].event_id


def test_rebase_uses_selected_disk_recovery_payload_for_duplicate_event_id(
    tmp_path: Path,
    monkeypatch,
):
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    real_write = admission_incidents.secure_write_root

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    admission_incidents.observe(
        admission_incidents.IntakeRestored("shadow", 1),
        notify=lambda _event: False,
        state_dir=tmp_path,
    )

    monkeypatch.setattr(admission_incidents, "secure_write_root", real_write)
    with admission_incidents._locked(tmp_path):
        disk = admission_incidents._read_unlocked(tmp_path)
        admission_incidents._apply_observation(
            disk,
            admission_incidents.IntakeRestored("shadow", 2),
            now=time.time(),
        )
        admission_incidents._write_unlocked(disk, tmp_path)

    events = []
    admission_incidents.observe(
        admission_incidents.Waiting(),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert len(events) == 1
    assert events[0].kind == "admission_mode_recovered"
    assert events[0].payload["effective_generation"] == 2
    assert admission_incidents.snapshot(state_dir=tmp_path)["last_effective_generation"] == 2


def test_waiting_never_replays_stale_memory_recovery_over_newer_disk_fault(
    tmp_path: Path,
    monkeypatch,
):
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    real_write = admission_incidents.secure_write_root

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    admission_incidents.observe(
        admission_incidents.IntakeRestored("shadow", 1),
        notify=lambda _event: False,
        state_dir=tmp_path,
    )

    monkeypatch.setattr(admission_incidents, "secure_write_root", real_write)
    with admission_incidents._locked(tmp_path):
        disk = admission_incidents._read_unlocked(tmp_path)
        admission_incidents._apply_observation(
            disk,
            admission_incidents.Faulted("state_unreadable", "shadow", 1),
            now=time.time(),
        )
        admission_incidents._write_unlocked(disk, tmp_path)

    admission_incidents.observe(
        admission_incidents.Waiting(),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    projection = admission_incidents.snapshot(state_dir=tmp_path)

    assert projection["active"] is True
    assert projection["error_code"] == "state_unreadable"


def test_reentrant_notifier_does_not_self_deadlock(tmp_path: Path):
    events = []
    nested_receipts = []

    def notify(event):
        events.append(event)
        if len(events) == 1:
            nested_receipts.append(
                admission_incidents.observe(
                    admission_incidents.Faulted("state_unreadable", "shadow", 1),
                    notify=notify,
                    state_dir=tmp_path,
                )
            )

    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            admission_incidents.observe(
                admission_incidents.Faulted("invalid_json", "shadow", 1),
                notify=notify,
                state_dir=tmp_path,
            )
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(result) == 1
    assert nested_receipts[0].notification == "deferred"
    assert nested_receipts[0].diagnostic == "notification_reentrant"
    assert [event.payload["error_code"] for event in events] == [
        "invalid_json",
        "state_unreadable",
    ]
    assert admission_incidents.snapshot(state_dir=tmp_path)["error_code"] == "state_unreadable"


def test_memory_notifier_reentry_preserves_nested_transition_without_duplicates(
    tmp_path: Path,
    monkeypatch,
):
    def fail_write(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    events = []
    nested_receipts = []

    def notify(event):
        events.append(event)
        if len(events) == 1:
            nested_receipts.append(
                admission_incidents.observe(
                    admission_incidents.Faulted("state_unreadable", "shadow", 1),
                    notify=notify,
                    state_dir=tmp_path,
                )
            )

    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            admission_incidents.observe(
                admission_incidents.Faulted("invalid_json", "shadow", 1),
                notify=notify,
                state_dir=tmp_path,
            )
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(result) == 1
    assert nested_receipts[0].notification == "deferred"
    assert nested_receipts[0].diagnostic == "notification_reentrant"
    assert [event.payload["error_code"] for event in events] == [
        "invalid_json",
        "state_unreadable",
    ]
    assert admission_incidents.snapshot(state_dir=tmp_path)["error_code"] == "state_unreadable"


def test_corrupt_ledger_falls_back_to_memory_and_still_pages(tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "admission_incident.json").write_text("{broken", encoding="utf-8")
    events = []

    first = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 9),
        notify=events.append,
        state_dir=tmp_path,
    )
    repeated = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 9),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert first.durability == repeated.durability == "memory_only"
    assert first.diagnostic == repeated.diagnostic == "ledger_invalid_json"
    assert first.notification == "queued"
    assert repeated.notification == "deduped"
    assert len(events) == 1
    assert admission_incidents.snapshot(state_dir=tmp_path)["active"] is True


def test_deeply_nested_json_falls_back_to_memory_and_still_pages(tmp_path: Path):
    nested = "[" * 10_000 + "0" + "]" * 10_000
    (tmp_path / "admission_incident.json").write_text(nested, encoding="utf-8")
    events = []

    receipt = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 9),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert receipt.durability == "memory_only"
    assert receipt.diagnostic == "ledger_invalid_json"
    assert receipt.notification == "queued"
    assert len(events) == 1
    assert admission_incidents.snapshot(state_dir=tmp_path)["active"] is True


def test_memory_fallback_dedupes_real_path_and_symlink_alias(
    tmp_path: Path,
    monkeypatch,
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    alias = tmp_path / "state-alias"
    alias.symlink_to(state_dir, target_is_directory=True)
    events = []

    def fail_write(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    first = admission_incidents.observe(
        admission_incidents.Faulted("state_write_failed", "shadow", 2),
        notify=events.append,
        state_dir=state_dir,
    )
    repeated = admission_incidents.observe(
        admission_incidents.Faulted("state_write_failed", "shadow", 2),
        notify=events.append,
        state_dir=alias,
    )

    assert repeated.incident_id == first.incident_id
    assert repeated.notification == "deduped"
    assert len(events) == 1


def test_untrusted_observation_is_normalized_before_persistence_or_notification(tmp_path: Path):
    events = []

    receipt = admission_incidents.observe(
        admission_incidents.Faulted(
            error_code="/root/private/control.json: token=secret",
            effective_mode="private-mode",
            effective_generation=float("inf"),  # type: ignore[arg-type]
        ),
        notify=events.append,
        state_dir=tmp_path,
    )

    assert receipt.phase == "open"
    assert len(events) == 1
    assert events[0].payload["error_code"] == "unknown_fault"
    assert events[0].payload["effective_mode"] == "shadow"
    assert events[0].payload["effective_generation"] == 0
    projection = admission_incidents.snapshot(state_dir=tmp_path)
    assert projection["error_code"] == "unknown_fault"
    assert "/root/" not in str(events[0].payload)
    assert "secret" not in str(events[0].payload)


def test_huge_generation_is_bounded_and_projection_remains_json_serializable(
    tmp_path: Path,
):
    events = []

    admission_incidents.observe(
        admission_incidents.Faulted(
            "invalid_json",
            "shadow",
            10**5_000,
        ),
        notify=events.append,
        state_dir=tmp_path,
    )
    projection = admission_incidents.snapshot(state_dir=tmp_path)

    assert events[0].payload["effective_generation"] == 0
    assert projection["last_effective_generation"] == 0
    json.dumps(projection, allow_nan=False)


@pytest.mark.parametrize(
    "ledger",
    [
        {
            "schema_version": 1,
            "sequence": 1,
            "incident": {"phase": "open"},
            "outbox": [],
        },
        {
            "schema_version": 1,
            "sequence": 1,
            "incident": {
                "id": "admission-" + "a" * 32,
                "phase": "open",
                "revision": 1,
                "error_code": "invalid_json",
                "first_seen_at": float("nan"),
                "last_seen_at": 1.0,
                "recovered_at": None,
                "last_effective_mode": "shadow",
                "last_effective_generation": 1,
            },
            "outbox": [],
        },
        {
            "schema_version": 1,
            "sequence": 0,
            "incident": None,
            "outbox": [
                {
                    "event_id": "forged",
                    "kind": "task_failed",
                    "title": "/root/secret",
                    "payload": {"token": "secret"},
                }
            ],
        },
        {
            "schema_version": 1,
            "sequence": 0,
            "incident": {
                "id": "admission-" + "a" * 32,
                "phase": "open",
                "revision": 7,
                "error_code": "invalid_json",
                "first_seen_at": 1.0,
                "last_seen_at": 2.0,
                "recovered_at": None,
                "last_effective_mode": "shadow",
                "last_effective_generation": 1,
            },
            "outbox": [],
        },
        {
            "schema_version": 1,
            "sequence": 2,
            "incident": {
                "id": "admission-" + "a" * 32,
                "phase": "recovered",
                "revision": 1,
                "error_code": "invalid_json",
                "first_seen_at": 1.0,
                "last_seen_at": 3.0,
                "recovered_at": 2.0,
                "last_effective_mode": "shadow",
                "last_effective_generation": 1,
            },
            "outbox": [],
        },
    ],
)
def test_malformed_ledger_returns_safe_snapshot_without_throwing(
    tmp_path: Path,
    ledger: dict,
):
    (tmp_path / "admission_incident.json").write_text(
        json.dumps(ledger),
        encoding="utf-8",
    )

    projection = admission_incidents.snapshot(state_dir=tmp_path)

    assert projection["active"] is False
    assert projection["incident_id"] is None
    assert projection["diagnostic"] == "ledger_invalid_schema"
    assert "/root/" not in str(projection)
    assert "secret" not in str(projection)


def test_wall_clock_rollback_does_not_corrupt_incident_ordering(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(admission_incidents.time, "time", lambda: 100.0)
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    monkeypatch.setattr(admission_incidents.time, "time", lambda: 50.0)

    repeated = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    projection = admission_incidents.snapshot(state_dir=tmp_path)

    assert repeated.durability == "durable"
    assert projection["active"] is True
    assert projection["first_seen_at"] == projection["last_seen_at"] == 100.0


def test_nonfinite_snapshot_clock_never_leaks_non_json_duration(
    tmp_path: Path,
    monkeypatch,
):
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "shadow", 1),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    monkeypatch.setattr(admission_incidents.time, "time", lambda: float("inf"))

    projection = admission_incidents.snapshot(state_dir=tmp_path)

    assert math.isfinite(projection["duration_s"])
    json.dumps(projection, allow_nan=False)
