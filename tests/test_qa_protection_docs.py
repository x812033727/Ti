"""QA 驗收：任務「更新文件說明分支保護檢查所需權限與無權限時的保守行為」一致性測試。

對應驗收標準 #7：README/.env.example 須註明
  - 環境變數 TI_AUTOPILOT_PROTECTION_CHECK（含預設啟用）；
  - 所需 token 權限 Administration:read（讀舊 protection 端點才需）；
  - 無權限／無法確認時的保守行為——一律「中止」、回含「無法確認保護狀態」字樣、不誤判放行；
  - 明確逃生口：設 0 整段跳過。

本測試屬文件一致性檢查，釘住成果避免日後文件腐化或與程式碼預設脫節。

比照 tests/test_qa_task6_docs.py 手法：讀檔做關鍵字斷言 + reload config 驗證預設一致。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from studio import config

_ROOT = Path(__file__).resolve().parent.parent
_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_ENV_EXAMPLE = (_ROOT / ".env.example").read_text(encoding="utf-8")
_CONFIG_PY = (_ROOT / "studio" / "config.py").read_text(encoding="utf-8")

_VAR = "TI_AUTOPILOT_PROTECTION_CHECK"


# === README：列出變數並涵蓋權限/保守行為/逃生口 =========================


def test_readme_lists_protection_check_var():
    assert _VAR in _README, "README 未說明 TI_AUTOPILOT_PROTECTION_CHECK"


def _readme_protection_line() -> str:
    return next(ln for ln in _README.splitlines() if _VAR in ln)


@pytest.mark.parametrize(
    "needle,why",
    [
        ("Administration:read", "須註明所需 token 權限"),
        ("無法確認保護狀態", "須說明回明確訊息字樣"),
        ("中止", "須說明無法確認時的保守行為＝中止"),
        ("Rulesets", "須說明優先打 Rulesets 端點"),
        ("逃生口", "須說明設 0 的逃生口"),
    ],
)
def test_readme_protection_line_covers(needle, why):
    line = _readme_protection_line()
    assert needle in line, f"README {_VAR} 說明缺『{needle}』（{why}）：{line}"


def test_readme_states_default_enabled():
    """README 須標明預設啟用（值 1）。"""
    line = _readme_protection_line()
    assert "1" in line and ("啟用" in line or "預設" in line), line


# === .env.example：列出變數並涵蓋權限/保守行為/逃生口 ===================


def test_env_example_lists_protection_check_var():
    assert _VAR in _ENV_EXAMPLE, ".env.example 未列出 TI_AUTOPILOT_PROTECTION_CHECK"


@pytest.mark.parametrize(
    "needle,why",
    [
        ("Administration:read", "須註明所需權限"),
        ("無法確認保護狀態", "須說明訊息字樣"),
        ("中止", "須說明保守中止行為"),
        ("逃生口", "須說明設 0 整段跳過的逃生口"),
        ("Rulesets", "須說明優先打 Rulesets 端點"),
    ],
)
def test_env_example_covers(needle, why):
    assert needle in _ENV_EXAMPLE, f".env.example 缺『{needle}』（{why}）"


# === 一致性：文件宣稱「預設啟用」必須等於程式碼預設 True ================


def test_docs_match_code_default_enabled():
    """乾淨環境（未設 env）下重載 config，PROTECTION_CHECK 預設為 True，呼應文件「預設啟用」。"""
    os.environ.pop(_VAR, None)
    try:
        importlib.reload(config)
        assert config.AUTOPILOT_PROTECTION_CHECK is True
    finally:
        importlib.reload(config)


def test_config_py_documents_protection_check():
    """config.py 定義處須有說明註解（用途 + 逃生口）。"""
    assert "AUTOPILOT_PROTECTION_CHECK" in _CONFIG_PY
    assert "fail-safe" in _CONFIG_PY or "中止" in _CONFIG_PY
