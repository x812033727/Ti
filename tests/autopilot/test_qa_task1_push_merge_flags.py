"""QA 驗收：任務 #1「config.py 新增推送安全旗標」專測。

驗收標準（聚焦本任務）：
- 旗標存在且型別為 bool：AUTOPILOT_FORCE_PUSH。
- 預設取「安全側」：未設環境變數時為 False（非強制推送）。
- 可由環境變數覆寫：TI_AUTOPILOT_FORCE_PUSH。
- 環境變數解析語意與其他旗標一致：("0","false","False","") 與未設定 → False；
  其餘任何值 → True。

註（2026-06-21）：合併改走 publisher._merge_flow（等 CI→綠才合併），原 MERGE_ADMIN
盲合旗標已徹底移除，故本檔僅保留 FORCE_PUSH 的推送安全旗標契約。
"""

from __future__ import annotations

import importlib
import os

import pytest

from studio import config

_ENVS = ("TI_AUTOPILOT_FORCE_PUSH",)


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


# === 預設安全側（驗收標準 5）==========================================


def test_defaults_are_safe_side():
    """未設環境變數時，旗標為 False（非強制推送）。"""
    _reload_clean()
    assert config.AUTOPILOT_FORCE_PUSH is False


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
def test_env_overrides_force_push(monkeypatch, val, expected):
    for env in _ENVS:
        if val is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, val)
    importlib.reload(config)
    assert config.AUTOPILOT_FORCE_PUSH is expected
