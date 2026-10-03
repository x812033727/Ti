"""Regression tests for project roots shadowed by /tmp tmpfs in bwrap."""

import shutil
import subprocess

import pytest

from studio import runner

TIMEOUT = 30

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="bwrap is not available",
)


@needs_bwrap
def test_tmp_project_root_rebind_keeps_lane_writable_but_parent_readonly(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    lane = project_root / "lane"
    lane.mkdir(parents=True)
    protected = project_root / "protected.txt"
    protected.write_text("ORIG", encoding="utf-8")
    monkeypatch.setattr(runner.config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(runner.config, "SANDBOX_NET", True)

    prefix = runner._bwrap_prefix(lane)
    result = subprocess.run(
        prefix
        + [
            "bash",
            "-c",
            (f"set -e; printf OK > made.txt; printf TAINT > {protected}"),
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Read-only file system" in result.stderr
    assert (lane / "made.txt").read_text(encoding="utf-8") == "OK"
    assert protected.read_text(encoding="utf-8") == "ORIG"
