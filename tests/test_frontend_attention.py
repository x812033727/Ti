"""「需要你」收件匣(軌 F1):node 驅動 frontend_attention_test.mjs。"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from _repo import REPO_ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_attention_mjs():
    cp = subprocess.run(
        ["node", str(REPO_ROOT / "tests" / "frontend_attention_test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert cp.returncode == 0 and "OK" in cp.stdout, cp.stdout + cp.stderr
