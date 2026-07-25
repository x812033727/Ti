"""跨程序 task-admission mode handshake 的公開介面契約。"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from studio import admission_mode

_PUBLIC_FIELDS = {
    "desired",
    "effective",
    "generation",
    "effective_generation",
    "pending",
    "healthy",
    "error",
    "released_holds",
}


def _assert_public_projection(state) -> None:
    public = state.to_public()
    assert _PUBLIC_FIELDS <= set(public)
    for field in _PUBLIC_FIELDS:
        assert public[field] == getattr(state, field)


def _state_json_path(state_dir: Path) -> Path:
    paths = list(state_dir.glob("*.json"))
    assert len(paths) == 1
    return paths[0]


def _bootstrap(mode: str, **kwargs):
    return admission_mode.bootstrap_at_task_boundary(
        mode,
        release_holds=lambda _mode: 0,
        **kwargs,
    )


def _request_process(state_dir: str, mode: str, gate, queue) -> None:
    """spawn worker：模擬另一個 web process 同時要求切換。"""
    from studio import admission_mode as process_admission_mode

    try:
        gate.wait(timeout=10)
        state = process_admission_mode.request(mode, state_dir=Path(state_dir))
        queue.put({"ok": True, "state": state.to_public()})
    except Exception as exc:  # pragma: no cover - 父行程會以 queue 診斷失敗
        queue.put({"ok": False, "error": repr(exc)})


def _reconcile_process(state_dir: str, queue) -> None:
    """spawn worker：模擬 autopilot process 在任務邊界讀取並 ack。"""
    from studio import admission_mode as process_admission_mode

    def release_holds(mode: str) -> int:
        return 0

    try:
        state = process_admission_mode.reconcile_at_task_boundary(
            release_holds=release_holds,
            state_dir=Path(state_dir),
        )
        queue.put({"ok": True, "state": state.to_public()})
    except Exception as exc:  # pragma: no cover - 父行程會以 queue 診斷失敗
        queue.put({"ok": False, "error": repr(exc)})


def _join_processes(processes) -> None:
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(2)
    assert [process.exitcode for process in processes] == [0] * len(processes)


def test_worker_boundary_bootstraps_desired_and_known_effective_mode(tmp_path: Path):
    state = _bootstrap(
        "enforce",
        state_dir=tmp_path,
        initial_effective="shadow",
    )

    assert state.desired == "enforce"
    assert state.effective == "shadow"
    assert state.generation == 2
    assert state.effective_generation == 1
    assert state.pending is True
    assert state.healthy is True
    assert not state.error
    assert state.released_holds == 0
    _assert_public_projection(state)

    reread = admission_mode.snapshot(state_dir=tmp_path, fallback_mode="off")
    assert reread.to_public() == state.to_public()


def test_web_request_cannot_initialize_missing_control_state(tmp_path: Path):
    with pytest.raises(admission_mode.AdmissionModeError) as raised:
        admission_mode.request("enforce", state_dir=tmp_path)

    assert raised.value.code == "not_initialized"
    state = admission_mode.snapshot(state_dir=tmp_path)
    assert state.healthy is False
    assert state.error == "not_initialized"


def test_worker_restart_does_not_overwrite_existing_shared_desired(tmp_path: Path):
    _bootstrap(
        "shadow",
        state_dir=tmp_path,
        initial_effective="shadow",
    )
    requested = admission_mode.request("enforce", state_dir=tmp_path)

    restarted = _bootstrap(
        "off",
        state_dir=tmp_path,
        initial_effective="off",
    )

    assert restarted == requested
    assert restarted.desired == "enforce"
    assert restarted.effective == "shadow"
    assert restarted.pending is True


def test_bootstrap_release_failure_leaves_control_state_uninitialized(tmp_path: Path):
    def fail_release(mode: str) -> int:
        assert mode == "shadow"
        raise OSError("backlog unavailable")

    with pytest.raises(admission_mode.AdmissionModeError) as raised:
        admission_mode.bootstrap_at_task_boundary(
            "shadow",
            state_dir=tmp_path,
            initial_effective="shadow",
            release_holds=fail_release,
        )

    assert raised.value.code == "hold_release_failed"
    state = admission_mode.snapshot(state_dir=tmp_path)
    assert state.healthy is False
    assert state.error == "not_initialized"


def test_requesting_the_existing_desired_mode_is_idempotent(tmp_path: Path):
    first = _bootstrap(
        "enforce",
        state_dir=tmp_path,
        initial_effective="shadow",
    )
    second = admission_mode.request("enforce", state_dir=tmp_path)

    assert second.desired == first.desired
    assert second.effective == first.effective
    assert second.generation == first.generation == 2
    assert second.effective_generation == first.effective_generation == 1
    assert second.pending is True


def test_upgrade_stays_pending_until_task_boundary_ack(tmp_path: Path):
    initial = _bootstrap(
        "shadow",
        state_dir=tmp_path,
        initial_effective="shadow",
    )
    assert initial.generation == initial.effective_generation == 1
    assert initial.pending is False

    requested = admission_mode.request("enforce", state_dir=tmp_path)
    assert requested.desired == "enforce"
    assert requested.effective == "shadow"
    assert requested.generation == 2
    assert requested.effective_generation == 1
    assert requested.pending is True

    release_calls = []

    def release_holds(mode: str) -> int:
        release_calls.append(mode)
        return 99

    acknowledged = admission_mode.reconcile_at_task_boundary(
        release_holds=release_holds,
        state_dir=tmp_path,
    )

    assert release_calls == []
    assert acknowledged.desired == acknowledged.effective == "enforce"
    assert acknowledged.generation == acknowledged.effective_generation == 2
    assert acknowledged.pending is False
    assert acknowledged.released_holds == 0


def test_downgrade_releases_holds_before_persisting_ack(tmp_path: Path):
    _bootstrap(
        "enforce",
        state_dir=tmp_path,
        initial_effective="enforce",
    )
    requested = admission_mode.request("shadow", state_dir=tmp_path)
    observed = []

    def release_holds(mode: str) -> int:
        # 這是唯一刻意讀 persistence JSON 的測試：驗證 callback 執行當下尚未 ack。
        raw = json.loads(_state_json_path(tmp_path).read_text(encoding="utf-8"))
        observed.append(
            {
                "mode": mode,
                "effective": raw["effective"],
                "effective_generation": raw["effective_generation"],
            }
        )
        return 3

    acknowledged = admission_mode.reconcile_at_task_boundary(
        release_holds=release_holds,
        state_dir=tmp_path,
    )

    assert observed == [
        {
            "mode": "shadow",
            "effective": "enforce",
            "effective_generation": 1,
        }
    ]
    assert requested.generation == 2
    assert acknowledged.desired == acknowledged.effective == "shadow"
    assert acknowledged.generation == acknowledged.effective_generation == 2
    assert acknowledged.pending is False
    assert acknowledged.released_holds == 3


def test_release_failure_leaves_downgrade_unacknowledged(tmp_path: Path):
    _bootstrap(
        "enforce",
        state_dir=tmp_path,
        initial_effective="enforce",
    )
    admission_mode.request("off", state_dir=tmp_path)

    def fail_release(mode: str) -> int:
        assert mode == "off"
        raise RuntimeError("backlog unavailable")

    with pytest.raises(admission_mode.AdmissionModeError):
        admission_mode.reconcile_at_task_boundary(
            release_holds=fail_release,
            state_dir=tmp_path,
        )

    current = admission_mode.snapshot(state_dir=tmp_path, fallback_mode="shadow")
    assert current.desired == "off"
    assert current.effective == "enforce"
    assert current.generation == 2
    assert current.effective_generation == 1
    assert current.pending is True


def test_ack_write_failure_keeps_generation_pending_for_idempotent_retry(
    tmp_path: Path,
    monkeypatch,
):
    _bootstrap(
        "enforce",
        state_dir=tmp_path,
        initial_effective="enforce",
    )
    admission_mode.request("shadow", state_dir=tmp_path)
    real_write = admission_mode.secure_write_root
    releases = []

    def fail_ack(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission_mode, "secure_write_root", fail_ack)
    with pytest.raises(admission_mode.AdmissionModeError) as raised:
        admission_mode.reconcile_at_task_boundary(
            release_holds=lambda mode: releases.append(mode) or 2,
            state_dir=tmp_path,
        )

    assert raised.value.code == "state_write_failed"
    pending = admission_mode.snapshot(state_dir=tmp_path)
    assert pending.effective == "enforce"
    assert pending.desired == "shadow"
    assert pending.pending is True

    monkeypatch.setattr(admission_mode, "secure_write_root", real_write)
    applied = admission_mode.reconcile_at_task_boundary(
        release_holds=lambda mode: releases.append(mode) or 0,
        state_dir=tmp_path,
    )

    assert releases == ["shadow", "shadow"]
    assert applied.effective == "shadow"
    assert applied.pending is False


def test_pending_enforce_request_can_be_canceled_back_to_effective_mode(tmp_path: Path):
    _bootstrap(
        "shadow",
        state_dir=tmp_path,
        initial_effective="shadow",
    )
    enforcing = admission_mode.request("enforce", state_dir=tmp_path)
    canceled = admission_mode.request("shadow", state_dir=tmp_path)

    assert enforcing.generation == 2
    assert canceled.desired == canceled.effective == "shadow"
    assert canceled.generation == 3
    assert canceled.effective_generation == 1
    assert canceled.pending is True

    release_calls = []
    acknowledged = admission_mode.reconcile_at_task_boundary(
        release_holds=lambda mode: release_calls.append(mode) or 0,
        state_dir=tmp_path,
    )

    assert release_calls == []
    assert acknowledged.desired == acknowledged.effective == "shadow"
    assert acknowledged.generation == acknowledged.effective_generation == 3
    assert acknowledged.pending is False


def test_corrupt_snapshot_is_unhealthy_and_reconcile_fails_safe(tmp_path: Path):
    _bootstrap(
        "shadow",
        state_dir=tmp_path,
        initial_effective="shadow",
    )
    _state_json_path(tmp_path).write_text("{not valid json", encoding="utf-8")

    state = admission_mode.snapshot(state_dir=tmp_path, fallback_mode="off")

    assert state.desired == state.effective == "off"
    assert state.generation == state.effective_generation == 0
    assert state.pending is False
    assert state.healthy is False
    assert state.error
    assert state.released_holds == 0
    _assert_public_projection(state)

    release_calls = []
    with pytest.raises(admission_mode.AdmissionModeError):
        admission_mode.reconcile_at_task_boundary(
            release_holds=lambda mode: release_calls.append(mode) or 0,
            state_dir=tmp_path,
        )
    assert release_calls == []


def test_two_spawn_process_requests_do_not_lose_a_generation(tmp_path: Path):
    _bootstrap(
        "shadow",
        state_dir=tmp_path,
        initial_effective="shadow",
    )
    process_context = multiprocessing.get_context("spawn")
    gate = process_context.Barrier(2)
    queue = process_context.Queue()
    processes = [
        process_context.Process(
            target=_request_process,
            args=(str(tmp_path), mode, gate, queue),
        )
        for mode in ("off", "enforce")
    ]

    _join_processes(processes)
    results = [queue.get(timeout=2) for _ in processes]

    assert all(result["ok"] for result in results), results
    states = [result["state"] for result in results]
    assert sorted(state["generation"] for state in states) == [2, 3]
    final = admission_mode.snapshot(state_dir=tmp_path, fallback_mode="shadow")
    last = next(state for state in states if state["generation"] == 3)
    assert final.generation == 3
    assert final.desired == last["desired"]
    assert final.effective == "shadow"
    assert final.effective_generation == 1
    assert final.pending is True


def test_spawn_autopilot_worker_reconcile_observes_web_request(tmp_path: Path):
    _bootstrap(
        "shadow",
        state_dir=tmp_path,
        initial_effective="shadow",
    )
    requested = admission_mode.request("enforce", state_dir=tmp_path)
    assert requested.pending is True

    process_context = multiprocessing.get_context("spawn")
    queue = process_context.Queue()
    process = process_context.Process(
        target=_reconcile_process,
        args=(str(tmp_path), queue),
    )

    _join_processes([process])
    result = queue.get(timeout=2)

    assert result["ok"] is True, result
    acknowledged = result["state"]
    assert acknowledged["desired"] == acknowledged["effective"] == "enforce"
    assert acknowledged["generation"] == acknowledged["effective_generation"] == 2
    assert acknowledged["pending"] is False
    final = admission_mode.snapshot(state_dir=tmp_path, fallback_mode="shadow")
    assert final.to_public() == acknowledged
