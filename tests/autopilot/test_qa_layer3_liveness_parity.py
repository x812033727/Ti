from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys

from _repo import REPO_ROOT

from studio import autopilot

LIVENESS = REPO_ROOT / "deploy" / "ti-layer3-liveness.py"
MONITOR = REPO_ROOT / "deploy" / "ti-layer3-monitor.sh"
NOW = 1_800_000_000.0
THRESHOLD = 180.0
FRESH = NOW - 5.0
STALE = NOW - 3600.0


def _load_layer3():
    spec = importlib.util.spec_from_file_location("ti_layer3_liveness", LIVENESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


layer3 = _load_layer3()


def _running(**overrides) -> dict:
    status = {
        "state": "running",
        "task_id": 42,
        "sleep_until": None,
        "updated_at": FRESH,
        "quota": {"claude": 12},
        "last_activity_at": FRESH,
        "current_expert": "senior",
        "turn_started_at": STALE,
        "workers": {"count": 5, "cpu_active": True},
    }
    status.update(overrides)
    return status


def _cases() -> list[tuple[str, dict]]:
    missing_updated = _running()
    del missing_updated["updated_at"]
    missing_last_activity = _running(workers={"count": 5, "cpu_active": False})
    del missing_last_activity["last_activity_at"]
    no_turn_fields = _running(last_activity_at=STALE, workers={"count": 5, "cpu_active": True})
    del no_turn_fields["current_expert"]
    del no_turn_fields["turn_started_at"]
    return [
        (
            "white_long_turn_cpu_active_not_killed",
            _running(last_activity_at=STALE, workers={"count": 5, "cpu_active": True}),
        ),
        (
            "white_activity_fresh_even_if_cpu_idle",
            _running(last_activity_at=FRESH, workers={"count": 5, "cpu_active": False}),
        ),
        (
            "white_cpu_none_activity_fresh_not_killed",
            _running(last_activity_at=FRESH, workers={"count": None, "cpu_active": None}),
        ),
        ("black_main_loop_stall_kills", _running(updated_at=STALE)),
        (
            "black_main_loop_stall_kills_regardless_of_worker_cpu",
            _running(updated_at=STALE, workers={"count": 5, "cpu_active": True}),
        ),
        ("black_missing_updated_at_kills", missing_updated),
        (
            "black_flip_cpu_active_false_kills",
            _running(last_activity_at=STALE, workers={"count": 5, "cpu_active": False}),
        ),
        (
            "black_cpu_none_activity_stale_kills",
            _running(last_activity_at=STALE, workers={"count": None, "cpu_active": None}),
        ),
        ("black_missing_last_activity_and_cpu_false_kills", missing_last_activity),
        (
            "sleep_state_alive_while_sleeping",
            {
                "state": "quota_sleep",
                "task_id": None,
                "sleep_until": NOW + 600.0,
                "updated_at": STALE,
                "quota": {"claude": 0},
            },
        ),
        (
            "sleep_state_overrun_kills",
            {
                "state": "budget_sleep",
                "task_id": None,
                "sleep_until": NOW - THRESHOLD - 10.0,
                "updated_at": STALE,
                "quota": {},
            },
        ),
        ("idle_fresh_is_alive", {"state": "idle", "updated_at": FRESH, "quota": {}}),
        ("idle_stale_updated_at_is_dead_main_loop", {"state": "idle", "updated_at": STALE}),
        ("turn_fields_never_affect_verdict", no_turn_fields),
    ]


def test_layer3_liveness_copy_matches_reference_for_rules_1_to_5():
    for name, status in _cases():
        copied = copy.deepcopy(status)
        expected = autopilot.liveness_verdict(
            status,
            now=NOW,
            stale_threshold_s=THRESHOLD,
        )
        got = layer3.liveness_verdict_copy(
            copied,
            now=NOW,
            stale_threshold_s=THRESHOLD,
        )
        assert got == expected, name


def test_layer3_liveness_self_test_runs():
    result = subprocess.run(
        [sys.executable, str(LIVENESS), "--self-test"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "white_long_turn_cpu_active" in result.stdout
    assert "black_cpu_idle_and_activity_stale" in result.stdout


def test_layer3_monitor_self_test_runs():
    result = subprocess.run(
        ["bash", str(MONITOR), "--self-test"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "self-test: ok" in result.stdout
