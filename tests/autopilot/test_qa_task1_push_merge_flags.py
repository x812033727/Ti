"""QA 驗收：任務 #1「config.py 新增推送/合併安全旗標」專測。

驗收標準（聚焦本任務）：
- 旗標存在且型別為 bool：AUTOPILOT_FORCE_PUSH / AUTOPILOT_MERGE_ADMIN。
- 預設取「安全側」：未設環境變數時兩者皆 False
  （非強制推送、不繞過分支保護）。
- 可由環境變數覆寫：TI_AUTOPILOT_FORCE_PUSH / TI_AUTOPILOT_MERGE_ADMIN。
- 環境變數解析語意與其他旗標一致：("0","false","False","") 與未設定 → False；
  其餘任何值 → True。
"""

from __future__ import annotations

import importlib
import os

import pytest

from studio import config

_ENVS = ("TI_AUTOPILOT_FORCE_PUSH", "TI_AUTOPILOT_MERGE_ADMIN")
_FLAGS = ("AUTOPILOT_FORCE_PUSH", "AUTOPILOT_MERGE_ADMIN")


def _reload_clean():
    for env in _ENVS:
        os.environ.pop(env, None)
    importlib.reload(config)


@pytest.fixture(autouse=True)
def _restore_env():
    """每個測試結束後清掉環境變數並重載，避免污染其他測試。"""
    yield
    _reload_clean()


# === 旗標存在且型別正確 ================================================


def test_flags_exist_and_are_bool():
    _reload_clean()
    assert isinstance(config.AUTOPILOT_FORCE_PUSH, bool)
    assert isinstance(config.AUTOPILOT_MERGE_ADMIN, bool)


# === 預設安全側（驗收標準 5）==========================================


def test_defaults_are_safe_side():
    """未設環境變數時，兩旗標皆 False（非強制、不繞過保護）。"""
    _reload_clean()
    assert config.AUTOPILOT_FORCE_PUSH is False
    assert config.AUTOPILOT_MERGE_ADMIN is False


# === 環境變數可覆寫 + 解析語意 ========================================


@pytest.mark.parametrize(
    "val,expected",
    [
        (None, False),  # 未設定 → 預設關閉（安全側）
        ("0", False),
        ("false", False),
        ("False", False),
        ("", False),
        ("1", True),
        ("true", True),
        ("yes", True),  # 任何非關閉值 → 啟用
    ],
)
def test_env_overrides_both_flags(monkeypatch, val, expected):
    for env in _ENVS:
        if val is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, val)
    importlib.reload(config)
    assert config.AUTOPILOT_FORCE_PUSH is expected
    assert config.AUTOPILOT_MERGE_ADMIN is expected


def test_flags_independent(monkeypatch):
    """兩旗標各自獨立，互不影響。"""
    monkeypatch.setenv("TI_AUTOPILOT_FORCE_PUSH", "1")
    monkeypatch.delenv("TI_AUTOPILOT_MERGE_ADMIN", raising=False)
    importlib.reload(config)
    assert config.AUTOPILOT_FORCE_PUSH is True
    assert config.AUTOPILOT_MERGE_ADMIN is False

    monkeypatch.delenv("TI_AUTOPILOT_FORCE_PUSH", raising=False)
    monkeypatch.setenv("TI_AUTOPILOT_MERGE_ADMIN", "1")
    importlib.reload(config)
    assert config.AUTOPILOT_FORCE_PUSH is False
    assert config.AUTOPILOT_MERGE_ADMIN is True
