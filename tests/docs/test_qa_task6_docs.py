"""QA 驗收：任務 #6「更新註解與文件說明新旗標用途與預設值」一致性測試。

任務 #6 屬文件/註解更新，無可執行邏輯；本測試以「文件一致性檢查」釘住成果，
避免日後旗標語意改變而文件腐化。驗證各處皆說明 FORCE_PUSH 旗標的用途與預設值，且
文件描述的預設值與程式碼實際預設值一致：
- config.py：FORCE_PUSH 定義處有解釋註解（用途 + 預設安全側）。
- autopilot.py：push 旗標使用處有解釋註解。
- README.md：環境變數表列出 TI_AUTOPILOT_FORCE_PUSH，標明預設值/安全側。
- .env.example：列出該變數並說明。
- 一致性：文件宣稱「預設安全側」== 程式碼預設 False。

註（2026-06-21）：MERGE_ADMIN 盲合旗標已徹底移除（合併改走 publisher._merge_flow
等 CI→綠才合併），故本檔的 MERGE_ADMIN 相關斷言一併移除。
"""

from __future__ import annotations

import importlib
import os

import pytest
from _repo import REPO_ROOT

from studio import config

_ROOT = REPO_ROOT
_CONFIG_PY = (_ROOT / "studio" / "config.py").read_text(encoding="utf-8")
_AUTOPILOT_PY = (_ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_ENV_EXAMPLE = (_ROOT / ".env.example").read_text(encoding="utf-8")


# === config.py：定義處有說明用途與預設 ================================


def test_config_py_documents_force_push():
    assert "AUTOPILOT_FORCE_PUSH" in _CONFIG_PY
    # 用途關鍵字：非強制 / 中止 / force-with-lease；預設關鍵字
    assert "--force-with-lease" in _CONFIG_PY
    assert "--force-if-includes" in _CONFIG_PY
    assert "預設" in _CONFIG_PY


# === autopilot.py：使用處有解釋註解 ===================================


def test_autopilot_py_has_push_flag_comment():
    # push 非強制策略註解
    assert "--force-with-lease" in _AUTOPILOT_PY
    assert "--force-if-includes" in _AUTOPILOT_PY
    # 不得殘留裸 push -f
    assert "push -f" not in _AUTOPILOT_PY


# === README.md：環境變數表列出旗標 + 預設值 ===========================


@pytest.mark.parametrize("var", ["TI_AUTOPILOT_FORCE_PUSH"])
def test_readme_documents_flag(var):
    assert var in _README, f"README 未說明 {var}"
    # 取該變數所在行，確認標明預設值（0 / 安全側）
    line = next(ln for ln in _README.splitlines() if var in ln)
    assert "0" in line and ("安全" in line or "預設" in line), f"README 未標明 {var} 預設值：{line}"


# === .env.example：列出兩旗標 =========================================


@pytest.mark.parametrize("var", ["TI_AUTOPILOT_FORCE_PUSH"])
def test_env_example_documents_flag(var):
    assert var in _ENV_EXAMPLE, f".env.example 未列出 {var}"


# === 一致性：文件宣稱「預設安全側」必須等於程式碼預設 False ===========


def test_docs_match_code_defaults():
    """乾淨環境下重載 config，FORCE_PUSH 預設 False，呼應文件「預設安全側」。"""
    for env in ("TI_AUTOPILOT_FORCE_PUSH",):
        os.environ.pop(env, None)
    try:
        importlib.reload(config)
        assert config.AUTOPILOT_FORCE_PUSH is False
    finally:
        importlib.reload(config)
