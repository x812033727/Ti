"""前端 autopilot activity 面板：用 node 載入真實 web/js/panels/autopilot.js，
驗證 ttft_s chip 只在有值時顯示，缺值的舊資料不會炸。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SMOKE = Path(__file__).resolve().parent / "frontend_autopilot_activity_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 執行前端 smoke")
def test_autopilot_activity_renders_optional_ttft_s():
    result = subprocess.run(
        ["node", str(_SMOKE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"前端 smoke 失敗：\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
