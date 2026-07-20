"""前端新 UI 面板（UI 改版新增）：用 node 載入真實 web/js 模組驗證。

涵蓋：角色管理、小組管理（含開場 group payload）、主題切換、
workflow 結構化 stage 卡片編輯器、建立專案 modal。
各 .mjs 為獨立 node process（模組快取不互染），詳細斷言在 .mjs 內。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent

_SUITES = [
    "frontend_roles_panel_test.mjs",
    "frontend_groups_panel_test.mjs",
    "frontend_theme_test.mjs",
    "frontend_stage_editor_test.mjs",
    "frontend_project_create_modal_test.mjs",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 執行前端 smoke")
@pytest.mark.parametrize("suite", _SUITES)
def test_frontend_ui_panel_suite(suite: str):
    result = subprocess.run(
        ["node", str(_HERE / suite)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"{suite} 失敗：\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
