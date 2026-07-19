from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release_body_structure.py"
VENV = ROOT / ".venv"


def test_check_release_body_structure_bare_invocation_from_non_repo_cwd(tmp_path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    # Must use the system python3, not .venv/bin/python: this repo is editable-installed
    # in .venv, so the import would still work after deleting the script bootstrap.
    python3 = shutil.which("python3")
    assert python3 is not None
    python3_path = Path(python3).resolve()
    assert VENV.resolve() not in python3_path.parents

    result = subprocess.run(
        [str(python3_path), str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
