"""Autopilot 與 task-admission mode control state 的整合邊界。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from studio import admission_mode, autopilot, backlog as backlog_module, config


def _bootstrap(mode: str, **kwargs):
    return admission_mode.bootstrap_at_task_boundary(
        mode,
        release_holds=lambda _mode: 0,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _runtime_state(tmp_path: Path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state_dir)
    monkeypatch.setattr(autopilot, "_admission_effective_mode_runtime", None)
    monkeypatch.setattr(autopilot, "_admission_mode_bootstrap_fault", "")
    monkeypatch.setattr(autopilot, "_task_running", False)
    monkeypatch.setattr(autopilot, "_sideline_task_info", None)
    monkeypatch.setattr(autopilot, "_sideline_admission_claiming", False)
    return state_dir


@pytest.mark.asyncio
async def test_idle_boundary_applies_upgrade_without_releasing_holds(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "shadow",
        state_dir=_runtime_state,
        initial_effective="shadow",
    )
    admission_mode.request("enforce", state_dir=_runtime_state)
    releases = []
    monkeypatch.setattr(
        autopilot.backlog,
        "release_admission_holds",
        lambda **kwargs: releases.append(kwargs) or 0,
    )

    assert await autopilot._reconcile_admission_mode_boundary() is True

    state = admission_mode.snapshot(state_dir=_runtime_state)
    assert state.desired == state.effective == "enforce"
    assert state.generation == state.effective_generation == 2
    assert autopilot._effective_admission_mode() == "enforce"
    assert releases == []


@pytest.mark.asyncio
async def test_running_lane_keeps_request_pending_and_stops_new_intake(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "shadow",
        state_dir=_runtime_state,
        initial_effective="shadow",
    )
    admission_mode.request("enforce", state_dir=_runtime_state)
    monkeypatch.setattr(autopilot, "_task_running", True)
    statuses = []
    sleeps = []
    monkeypatch.setattr(
        autopilot,
        "_write_status",
        lambda state, **fields: statuses.append((state, fields)),
    )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(autopilot.asyncio, "sleep", fake_sleep)

    assert await autopilot._reconcile_admission_mode_boundary() is False

    state = admission_mode.snapshot(state_dir=_runtime_state)
    assert state.effective == "shadow"
    assert state.desired == "enforce"
    assert state.pending is True
    assert statuses[0][0] == "mode_switch_wait"
    assert sleeps == [5]


@pytest.mark.asyncio
async def test_idle_downgrade_releases_backlog_holds_before_ack(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "enforce",
        state_dir=_runtime_state,
        initial_effective="enforce",
    )
    admission_mode.request("off", state_dir=_runtime_state)
    observations = []

    def release_holds(*, mode):
        before_ack = admission_mode.snapshot(state_dir=_runtime_state)
        observations.append((mode, before_ack.effective, before_ack.pending))
        return 4

    monkeypatch.setattr(autopilot.backlog, "release_admission_holds", release_holds)

    assert await autopilot._reconcile_admission_mode_boundary() is True

    state = admission_mode.snapshot(state_dir=_runtime_state)
    assert observations == [("off", "enforce", True)]
    assert state.desired == state.effective == "off"
    assert state.pending is False
    assert state.released_holds == 4
    assert autopilot._effective_admission_mode() == "off"


@pytest.mark.asyncio
async def test_corrupt_control_state_fails_closed_with_fault_heartbeat(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "enforce",
        state_dir=_runtime_state,
        initial_effective="enforce",
    )
    (_runtime_state / "admission_mode.json").write_text("{broken", encoding="utf-8")
    statuses = []
    sleeps = []
    monkeypatch.setattr(
        autopilot,
        "_write_status",
        lambda state, **fields: statuses.append((state, fields)),
    )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(autopilot.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        autopilot.backlog,
        "release_admission_holds",
        lambda **_kwargs: pytest.fail("壞 control state 不得碰 backlog"),
    )

    assert await autopilot._reconcile_admission_mode_boundary() is False
    assert statuses[0][0] == "admission_mode_fault"
    assert statuses[0][1]["admission_mode_state"]["healthy"] is False
    assert sleeps == [60]


@pytest.mark.asyncio
async def test_missing_control_state_is_rebuilt_only_from_worker_runtime_pin(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "enforce",
        state_dir=_runtime_state,
        initial_effective="enforce",
    )
    monkeypatch.setattr(autopilot, "_admission_effective_mode_runtime", "enforce")
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "shadow")
    (_runtime_state / "admission_mode.json").unlink()

    assert await autopilot._reconcile_admission_mode_boundary() is True

    recovered = admission_mode.snapshot(state_dir=_runtime_state)
    assert recovered.healthy is True
    assert recovered.desired == recovered.effective == "enforce"
    assert recovered.pending is False


@pytest.mark.asyncio
async def test_restart_recovery_releases_old_enforce_holds_before_shadow_bootstrap(
    _runtime_state: Path,
    monkeypatch,
):
    initial = _bootstrap(
        "enforce",
        state_dir=_runtime_state,
        initial_effective="enforce",
    )
    task = backlog_module.add("舊 enforce hold", state_dir=_runtime_state)
    committed, error = backlog_module.commit_admission(
        task["id"],
        backlog_module.task_fingerprint(task),
        contract={"version": 1},
        admission={"mode": "enforce", "outcome": "blocked"},
        transition="park",
        state_dir=_runtime_state,
        expected_mode="enforce",
        expected_mode_generation=initial.effective_generation,
    )
    assert committed is not None and error == ""
    admission_mode.request("shadow", state_dir=_runtime_state)

    (_runtime_state / "admission_mode.json").unlink()
    monkeypatch.setattr(autopilot, "_admission_effective_mode_runtime", None)
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "shadow")

    assert await autopilot._reconcile_admission_mode_boundary() is True

    recovered = admission_mode.snapshot(state_dir=_runtime_state)
    released = backlog_module.get(task["id"], state_dir=_runtime_state)
    assert recovered.desired == recovered.effective == "shadow"
    assert recovered.released_holds == 1
    assert released["status"] == "pending"
    assert released["admission"]["released_by_mode"] == "shadow"


@pytest.mark.asyncio
async def test_bootstrap_fault_latch_stops_main_and_sideline_claims(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "shadow",
        state_dir=_runtime_state,
        initial_effective="shadow",
    )
    monkeypatch.setattr(autopilot, "_admission_mode_bootstrap_fault", "state_write_failed")
    statuses = []
    sleeps = []

    def fail_bootstrap(*_args, **_kwargs):
        raise admission_mode.AdmissionModeError("state_write_failed")

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(admission_mode, "bootstrap_at_task_boundary", fail_bootstrap)
    monkeypatch.setattr(
        autopilot,
        "_write_status",
        lambda state, **fields: statuses.append((state, fields)),
    )
    monkeypatch.setattr(autopilot.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        autopilot.backlog,
        "claim_next",
        lambda *_args, **_kwargs: pytest.fail("bootstrap fault 時不得 legacy claim"),
    )
    monkeypatch.setattr(
        autopilot,
        "_claim_next_with_admission",
        lambda *_args, **_kwargs: pytest.fail("bootstrap fault 時不得 admission claim"),
    )

    assert await autopilot._reconcile_admission_mode_boundary() is False
    task, mode = await autopilot._claim_sideline_at_effective_mode()

    assert task is None
    assert mode == "shadow"
    assert statuses[0][0] == "admission_mode_fault"
    assert sleeps == [60]


@pytest.mark.asyncio
async def test_sideline_does_not_claim_while_generation_is_pending(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "shadow",
        state_dir=_runtime_state,
        initial_effective="shadow",
    )
    admission_mode.request("enforce", state_dir=_runtime_state)
    monkeypatch.setattr(
        autopilot,
        "_claim_next_with_admission",
        lambda *_args, **_kwargs: pytest.fail("pending generation 不得進 admission claim"),
    )
    monkeypatch.setattr(
        autopilot.backlog,
        "claim_next",
        lambda *_args, **_kwargs: pytest.fail("pending generation 不得走 legacy claim"),
    )

    task, mode = await autopilot._claim_sideline_at_effective_mode()

    assert task is None
    assert mode == "shadow"
    assert autopilot._sideline_admission_claiming is False


def test_request_waits_for_old_generation_commit_then_downgrade_releases_it(
    _runtime_state: Path,
    monkeypatch,
):
    """重現 release-sweep race：request 返回前必須 drain 已驗證的舊 commit。"""
    initial = _bootstrap(
        "enforce",
        state_dir=_runtime_state,
        initial_effective="enforce",
    )
    task = backlog_module.add("延遲 enforce park", state_dir=_runtime_state)
    fingerprint = backlog_module.task_fingerprint(task)
    validated = threading.Event()
    allow_commit = threading.Event()
    original_matches = backlog_module._mode_generation_matches

    def pause_after_validation(**kwargs):
        matched = original_matches(**kwargs)
        if matched:
            validated.set()
            assert allow_commit.wait(5)
        return matched

    monkeypatch.setattr(backlog_module, "_mode_generation_matches", pause_after_validation)
    commit_result = []
    request_result = []

    def delayed_commit():
        commit_result.append(
            backlog_module.commit_admission(
                task["id"],
                fingerprint,
                contract={"version": 1},
                admission={"mode": "enforce", "outcome": "blocked"},
                transition="park",
                state_dir=_runtime_state,
                expected_mode="enforce",
                expected_mode_generation=initial.effective_generation,
            )
        )

    def request_off():
        request_result.append(admission_mode.request("off", state_dir=_runtime_state))

    commit_thread = threading.Thread(target=delayed_commit)
    request_thread = threading.Thread(target=request_off)
    commit_thread.start()
    assert validated.wait(5)
    request_thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if admission_mode.snapshot(state_dir=_runtime_state).pending:
            break
        time.sleep(0.01)
    assert admission_mode.snapshot(state_dir=_runtime_state).pending is True
    assert request_thread.is_alive(), "request 必須等已驗證的舊 backlog commit 結束"

    allow_commit.set()
    commit_thread.join(5)
    request_thread.join(5)
    assert not commit_thread.is_alive() and not request_thread.is_alive()
    assert commit_result[0][0] is not None
    assert request_result[0].desired == "off"

    acknowledged = admission_mode.reconcile_at_task_boundary(
        release_holds=lambda mode: backlog_module.release_admission_holds(
            mode=mode,
            state_dir=_runtime_state,
        ),
        state_dir=_runtime_state,
    )
    released = backlog_module.get(task["id"], state_dir=_runtime_state)
    assert acknowledged.effective == "off"
    assert released["status"] == "pending"
    assert released["admission"]["released_by_mode"] == "off"


def test_pending_request_rejects_old_generation_claim(
    _runtime_state: Path,
):
    initial = _bootstrap(
        "shadow",
        state_dir=_runtime_state,
        initial_effective="shadow",
    )
    task = backlog_module.add("不得越過 generation", state_dir=_runtime_state)
    admission_mode.request("enforce", state_dir=_runtime_state)

    committed, error = backlog_module.commit_admission(
        task["id"],
        backlog_module.task_fingerprint(task),
        contract={"version": 1},
        admission={"mode": "shadow", "outcome": "ready"},
        transition="claim",
        state_dir=_runtime_state,
        expected_mode="shadow",
        expected_mode_generation=initial.effective_generation,
    )

    assert committed is None
    assert error == "mode_changed"
    assert backlog_module.get(task["id"], state_dir=_runtime_state)["status"] == "pending"


def test_web_process_without_runtime_pin_reads_shared_effective(
    _runtime_state: Path,
    monkeypatch,
):
    _bootstrap(
        "shadow",
        state_dir=_runtime_state,
        initial_effective="shadow",
    )
    pending = admission_mode.request("enforce", state_dir=_runtime_state)
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    monkeypatch.setattr(autopilot, "_admission_effective_mode_runtime", None)

    assert pending.effective == "shadow"
    assert autopilot._effective_admission_mode() == "shadow"
    assert autopilot._core_enqueue_mode() == ("shadow", pending.effective_generation)
