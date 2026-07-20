"""前端監控儀表板：用 node 載入真實 web/js/panels/dashboard.js 驗證渲染與視圖切換。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SMOKE = Path(__file__).resolve().parent / "frontend_dashboard_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 執行前端 smoke")
def test_frontend_dashboard_panel():
    result = subprocess.run(
        ["node", str(_SMOKE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"前端 smoke 失敗：\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
