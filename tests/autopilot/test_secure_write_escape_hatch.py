"""任務 #1/#5 契約：config 三態解析、env_bool、require_chown 旁路與文件斷言。

對應驗收標準 #5/#7：
- config：預設與錯值皆得 strict、三態與同義詞正規化正確、env_bool 行為符合；
  顯式降級記「降級」、錯值記「無法辨識」warning、預設 strict 不記 warning。
- require_chown 參數（安全邊界旁路）非 None 時記 warning 稽核。
- 文件：README 與 .env.example 含 TI_REQUIRE_CHOWN、strict、Breaking change/breaking、warn、root。
"""

from __future__ import annotations

import importlib
import logging
import re
from pathlib import Path

import pytest

from studio import config, secure_write

ROOT = Path(__file__).resolve().parents[2]


def _reload_with(monkeypatch, value):
    """設 TI_REQUIRE_CHOWN 後 importlib.reload(config)，回傳重載後模式。"""
    if value is None:
        monkeypatch.delenv("TI_REQUIRE_CHOWN", raising=False)
    else:
        monkeypatch.setenv("TI_REQUIRE_CHOWN", value)
    importlib.reload(config)
    return config


# ---- env_bool ----


def test_env_bool_unset_returns_default(monkeypatch):
    monkeypatch.delenv("TI_X_BOOL", raising=False)
    assert config.env_bool("TI_X_BOOL", True) is True
    assert config.env_bool("TI_X_BOOL", False) is False


def test_env_bool_empty_returns_default(monkeypatch):
    monkeypatch.setenv("TI_X_BOOL", "")
    assert config.env_bool("TI_X_BOOL", True) is True


@pytest.mark.parametrize("val", ["0", "false", "False"])
def test_env_bool_false_values(monkeypatch, val):
    monkeypatch.setenv("TI_X_BOOL", val)
    assert config.env_bool("TI_X_BOOL", True) is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_env_bool_true_values(monkeypatch, val):
    monkeypatch.setenv("TI_X_BOOL", val)
    assert config.env_bool("TI_X_BOOL", False) is True


# ---- 三態解析 + fail-safe ----


def test_modes_constant():
    assert config.REQUIRE_CHOWN_MODES == ("strict", "warn", "off")


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "strict"),  # 未設 → 預設 strict
        ("", "strict"),  # 留空 → strict
        ("strict", "strict"),
        ("warn", "warn"),
        ("off", "off"),
        ("0", "off"),  # 布林假值同義 off
        ("false", "off"),
        ("False", "off"),  # ADR 正式假值（大小寫）
        ("bogus", "strict"),  # 無法辨識 → fail-safe strict
        ("1", "strict"),  # 真值但非合法模式 → fail-safe strict
    ],
)
def test_three_state_parse(monkeypatch, value, expected):
    cfg = _reload_with(monkeypatch, value)
    assert cfg.require_chown_mode() == expected
    assert cfg.REQUIRE_CHOWN == expected


def test_default_strict_no_warning(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        _reload_with(monkeypatch, None)
    assert not any("TI_REQUIRE_CHOWN" in r.message for r in caplog.records)


@pytest.mark.parametrize("value", ["warn", "off"])
def test_downgrade_logs_downgrade(monkeypatch, caplog, value):
    with caplog.at_level(logging.WARNING):
        _reload_with(monkeypatch, value)
    assert any("降級" in r.message for r in caplog.records)


def test_unknown_logs_unrecognized(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        _reload_with(monkeypatch, "bogus")
    assert any("無法辨識" in r.message for r in caplog.records)


def teardown_module(module):
    """還原 config 預設模組狀態（conftest 設 off）。"""
    import os

    os.environ["TI_REQUIRE_CHOWN"] = "off"
    importlib.reload(config)


# ---- require_chown 旁路：非 None 記 warning ----


def test_require_chown_override_logs(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        secure_write.secure_write_root(tmp_path / "a", b"x", require_chown="off")
    assert any("require_chown" in r.message for r in caplog.records)


# ---- 文件斷言（驗收標準 #7）----


@pytest.fixture(scope="module")
def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example():
    return (ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.mark.parametrize("token", ["TI_REQUIRE_CHOWN", "strict", "warn", "root", "off"])
def test_readme_tokens(readme, token):
    assert token in readme


@pytest.mark.parametrize("token", ["TI_REQUIRE_CHOWN", "strict", "warn", "root", "off"])
def test_env_tokens(env_example, token):
    assert token in env_example


def test_breaking_change_marked(readme, env_example):
    assert re.search(r"[Bb]reaking change", readme)
    assert re.search(r"[Bb]reaking change", env_example)
