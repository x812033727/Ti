from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from _repo import REPO_ROOT

MONITOR = REPO_ROOT / "deploy" / "ti-layer3-monitor.sh"
LIVENESS = REPO_ROOT / "deploy" / "ti-layer3-liveness.py"


def _workspace_tmp(name: str) -> Path:
    path = REPO_ROOT / f".qa-{name}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    return path


def _run_monitor_round(
    root: Path,
    *,
    status: dict,
) -> subprocess.CompletedProcess[str]:
    workdir = root / "work"
    state_dir = root / "state"
    status_file = root / "status.json"
    log_file = root / "host-calls.log"
    workdir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    status_file.write_text(json.dumps(status), encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "TI_LAYER3_WORKDIR": str(workdir),
            "TI_LAYER3_STATE_DIR": str(state_dir),
            "TI_LAYER3_STATUS_FILE": str(status_file),
            "TI_LAYER3_LIVENESS_SCRIPT": str(LIVENESS),
            "TI_LAYER3_PAUSE_FILE": str(root / "not-paused"),
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
bash {shlex.quote(str(MONITOR))}
"""
    return subprocess.run(
        ["bash", "-c", harness],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def _long_task_status(*, cpu_active: bool, updated_at: float) -> dict:
    frozen_activity = updated_at - 3600.0
    return {
        "state": "running",
        "task_id": 342,
        "sleep_until": None,
        "updated_at": updated_at,
        "quota": {"claude": 63.0, "codex": 36.0},
        "last_activity_at": frozen_activity,
        "workers": {"count": 38, "cpu_active": cpu_active},
        "current_expert": "qa",
        "turn_started_at": frozen_activity,
    }


def test_layer3_monitor_three_consecutive_rounds_do_not_restart_long_cpu_task():
    root = _workspace_tmp("layer3-task4-three-rounds")
    try:
        outputs: list[str] = []
        for _ in range(3):
            now = time.time()
            result = _run_monitor_round(
                root,
                status=_long_task_status(cpu_active=True, updated_at=now - 5.0),
            )
            outputs.append(result.stdout + result.stderr)
            assert result.returncode == 0, outputs[-1]

        evidence = "\n".join(outputs)
        assert evidence.count("layer3: all green") == 3
        assert evidence.count("verdict=alive") == 3
        assert evidence.count("cpu_active=true") == 3
        assert "layer3: 判死" not in evidence
        assert "layer3: 異常 → 喚起 Claude 診斷" not in evidence
        assert "liveness probe warning" not in evidence

        ages = [int(value) for value in re.findall(r"last_activity_age_s=(\d+)", evidence)]
        assert len(ages) == 3
        assert all(age >= 300 for age in ages)

        calls = (root / "host-calls.log").read_text(encoding="utf-8")
        assert "systemctl restart" not in calls
        assert "claude invoked" not in calls
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_layer3_monitor_same_long_task_restarts_when_cpu_is_idle():
    root = _workspace_tmp("layer3-task4-idle-boundary")
    try:
        now = time.time()
        result = _run_monitor_round(
            root,
            status=_long_task_status(cpu_active=False, updated_at=now - 5.0),
        )

        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert "verdict=dead_task" in output
        assert "last_activity_age_s=" in output
        assert "cpu_active=false" in output
        assert "layer3: 判死 → restart ti-autopilot.service" in output
        assert "layer3: 異常 → 喚起 Claude 診斷" not in output

        calls = (root / "host-calls.log").read_text(encoding="utf-8")
        assert "systemctl restart ti-autopilot.service" in calls
        assert "claude invoked" not in calls
    finally:
        shutil.rmtree(root, ignore_errors=True)
