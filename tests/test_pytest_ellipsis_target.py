import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_accepts_literal_ellipsis_as_default_test_target():
    env = os.environ.copy()
    env["CI"] = "1"
    result = subprocess.run(
        [
            str(ROOT / "python"),
            "-m",
            "pytest",
            "-q",
            "...",
            "--collect-only",
            "-k",
            "test_pytest_accepts_literal_ellipsis_as_default_test_target",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no tests ran" not in result.stdout
    assert Path(__file__).name in result.stdout
