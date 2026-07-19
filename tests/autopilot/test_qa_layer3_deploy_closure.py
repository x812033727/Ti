from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest
from _repo import REPO_ROOT

pytestmark = pytest.mark.skipif(
    os.environ.get("LAYER3_DEPLOY_QA") != "1",
    reason="set LAYER3_DEPLOY_QA=1 to verify the live systemd deployment",
)

REPO_MONITOR = REPO_ROOT / "deploy" / "ti-layer3-monitor.sh"
REPO_LIVENESS = REPO_ROOT / "deploy" / "ti-layer3-liveness.py"
SBIN_MONITOR = Path("/usr/local/sbin/ti-layer3-monitor.sh")
SBIN_LIVENESS = Path("/usr/local/sbin/ti-layer3-liveness.py")
TIMER_UNIT = "ti-layer3-monitor.timer"
SERVICE_UNIT = "ti-layer3-monitor.service"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _must_run(cmd: list[str]) -> str:
    result = _run(cmd)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _props(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def test_sbin_scripts_match_repo_and_are_executable():
    for repo_path, sbin_path in [
        (REPO_MONITOR, SBIN_MONITOR),
        (REPO_LIVENESS, SBIN_LIVENESS),
    ]:
        assert repo_path.is_file(), repo_path
        assert sbin_path.is_file(), sbin_path
        assert _sha256(sbin_path) == _sha256(repo_path)
        mode = sbin_path.stat().st_mode
        assert mode & stat.S_IXUSR, oct(mode)


def test_deployed_monitor_self_test_uses_sbin_liveness_copy():
    result = _run(["bash", str(SBIN_MONITOR), "--self-test"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "white_long_turn_cpu_active" in result.stdout
    assert "black_cpu_idle_and_activity_stale" in result.stdout
    assert "self-test: ok" in result.stdout


def test_timer_is_active_and_unit_contract_was_not_repointed():
    show = _must_run(
        [
            "systemctl",
            "show",
            TIMER_UNIT,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "UnitFileState",
            "-p",
            "LastTriggerUSec",
            "--no-pager",
        ]
    )
    props = _props(show)
    assert props["ActiveState"] == "active"
    assert props["SubState"] == "waiting"
    assert props["UnitFileState"] in {"enabled", "enabled-runtime"}
    assert props["LastTriggerUSec"]
    assert props["LastTriggerUSec"] != "n/a"

    unit = _must_run(["systemctl", "cat", SERVICE_UNIT, TIMER_UNIT, "--no-pager"])
    assert "ExecStart=/usr/local/sbin/ti-layer3-monitor.sh" in unit
    assert "OnBootSec=5min" in unit
    assert "OnUnitActiveSec=15min" in unit
    assert "RandomizedDelaySec=60" in unit
    assert "ExecStart=/opt/ti/deploy/ti-layer3-monitor.sh" not in unit


def test_journal_has_post_deploy_all_green_and_no_restart():
    deployed_at = int(min(SBIN_MONITOR.stat().st_mtime, SBIN_LIVENESS.stat().st_mtime))
    journal = _must_run(
        [
            "journalctl",
            "-u",
            SERVICE_UNIT,
            "--since",
            f"@{deployed_at}",
            "--no-pager",
            "-o",
            "cat",
        ]
    )
    assert "layer3: all green" in journal
    assert "liveness: verdict=alive" in journal
    assert "layer3: 判死" not in journal
    assert "systemctl restart" not in journal
    assert "layer3: 異常 → 喚起 Claude 診斷" not in journal
