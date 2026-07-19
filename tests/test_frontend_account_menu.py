"""帳號選單+靈感區(PR12):node 驅動 frontend_account_menu_test.mjs。"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from _repo import REPO_ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_account_menu_mjs():
    cp = subprocess.run(
        ["node", str(REPO_ROOT / "tests" / "frontend_account_menu_test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert cp.returncode == 0 and "OK" in cp.stdout, cp.stdout + cp.stderr
