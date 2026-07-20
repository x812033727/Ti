"""助手首頁對話(PR4):node 驅動 frontend_home_chat_test.mjs。"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from _repo import REPO_ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_home_chat_mjs():
    cp = subprocess.run(
        ["node", str(REPO_ROOT / "tests" / "frontend_home_chat_test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert cp.returncode == 0 and "OK" in cp.stdout, cp.stdout + cp.stderr
