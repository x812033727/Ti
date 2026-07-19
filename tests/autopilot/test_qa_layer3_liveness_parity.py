from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import time

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


def test_layer3_evaluate_status_probe_fail_is_null_safe():
    for status, reason in [
        ({}, "status_has_no_liveness_fields"),
        ([], "status_not_object"),
        (
            {
                "state": "",
                "updated_at": True,
                "sleep_until": False,
                "last_activity_at": False,
                "workers": "old_status_without_worker_dict",
            },
            "status_has_no_liveness_fields",
        ),
    ]:
        verdict, line = layer3.evaluate_status(
            status,
            now=NOW,
            stale_threshold_s=THRESHOLD,
        )
        assert verdict == "probe_fail"
        assert f"reason={reason}" in line


def test_layer3_cli_probe_fail_exits_zero_for_missing_or_bad_status(tmp_path):
    missing = tmp_path / "missing-status.json"
    bad = tmp_path / "bad-status.json"
    bad.write_text("{not-json", encoding="utf-8")

    for status_file, reason in [
        (missing, "status_file_missing"),
        (bad, "status_read_failed_JSONDecodeError"),
    ]:
        result = subprocess.run(
            [
                sys.executable,
                str(LIVENESS),
                "--status-file",
                str(status_file),
                "--now",
                str(NOW),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"verdict=probe_fail reason={reason}" in result.stdout


def test_layer3_cli_dead_task_exit_code_is_machine_readable(tmp_path):
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            _running(
                updated_at=FRESH,
                last_activity_at=STALE,
                workers={"count": 5, "cpu_active": False},
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(LIVENESS),
            "--status-file",
            str(status_file),
            "--now",
            str(NOW),
            "--stale-threshold-s",
            "300",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "verdict=dead_task" in result.stdout
    assert "cpu_active=false" in result.stdout


def test_layer3_cli_stale_threshold_is_clamped_to_300_seconds(tmp_path):
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps({"state": "idle", "updated_at": NOW - 250.0}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(LIVENESS),
            "--status-file",
            str(status_file),
            "--now",
            str(NOW),
            "--stale-threshold-s",
            "1",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verdict=alive" in result.stdout
    assert "updated_age_s=250" in result.stdout


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


def _run_monitor_with_stubbed_host_commands(
    tmp_path,
    *,
    status: dict | None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    workdir = tmp_path / "work"
    state_dir = tmp_path / "state"
    status_file = tmp_path / "status.json"
    log_file = tmp_path / "host-calls.log"
    workdir.mkdir()
    state_dir.mkdir()
    if status is not None:
        status_file.write_text(json.dumps(status), encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "TI_LAYER3_WORKDIR": str(workdir),
            "TI_LAYER3_STATE_DIR": str(state_dir),
            "TI_LAYER3_STATUS_FILE": str(status_file),
            "TI_LAYER3_LIVENESS_SCRIPT": str(LIVENESS),
            "TI_LAYER3_PAUSE_FILE": str(tmp_path / "not-paused"),
            "TI_LAYER3_TEST_LOG": str(log_file),
        }
    )
    harness = f"""
set -eu
systemctl() {{
  printf 'systemctl' >> "$TI_LAYER3_TEST_LOG"
  for arg in "$@"; do printf ' %s' "$arg" >> "$TI_LAYER3_TEST_LOG"; done
  printf '\\n' >> "$TI_LAYER3_TEST_LOG"
  case "${{1:-}}" in
    is-active|restart) return 0 ;;
    show) printf '0\\n'; return 0 ;;
  esac
  return 0
}}
curl() {{
  printf 'curl' >> "$TI_LAYER3_TEST_LOG"
  for arg in "$@"; do printf ' %s' "$arg" >> "$TI_LAYER3_TEST_LOG"; done
  printf '\\n' >> "$TI_LAYER3_TEST_LOG"
  return 0
}}
claude() {{
  printf 'claude invoked\\n' >> "$TI_LAYER3_TEST_LOG"
  return 0
}}
export -f systemctl curl claude
exec bash {MONITOR}
"""
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    calls = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    return result, calls


def test_layer3_monitor_does_not_restart_long_turn_with_cpu_active(tmp_path):
    now = time.time()
    result, calls = _run_monitor_with_stubbed_host_commands(
        tmp_path,
        status=_running(
            updated_at=now - 5.0,
            last_activity_at=now - 3600.0,
            workers={"count": 5, "cpu_active": True},
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "layer3: all green" in result.stdout
    assert "verdict=alive" in result.stdout
    assert "systemctl restart" not in calls
    assert "claude invoked" not in calls


def test_layer3_monitor_probe_fail_warns_without_restart(tmp_path):
    result, calls = _run_monitor_with_stubbed_host_commands(tmp_path, status=None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "liveness probe warning: verdict=probe_fail reason=status_file_missing" in result.stdout
    assert "systemctl restart" not in calls
    assert "claude invoked" not in calls


def test_layer3_monitor_dead_task_restarts_service_without_claude(tmp_path):
    now = time.time()
    result, calls = _run_monitor_with_stubbed_host_commands(
        tmp_path,
        status=_running(
            updated_at=now - 5.0,
            last_activity_at=now - 3600.0,
            workers={"count": 5, "cpu_active": False},
        ),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "layer3: 判死 → restart ti-autopilot.service: verdict=dead_task" in result.stdout
    assert "layer3: restart ti-autopilot.service exit=0" in result.stdout
    assert "systemctl restart ti-autopilot.service" in calls
    assert "claude invoked" not in calls
