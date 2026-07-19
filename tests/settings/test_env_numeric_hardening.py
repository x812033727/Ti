"""數字欄空值硬化(2026-07-19 事故回歸):
設定面板把空的數字欄寫成 KEY='' 進 .env → config import/reload 的 int('') 炸 →
web 儲存 500、autopilot 重啟即 crashloop。

守護不變量:
- settings.update:數字欄空值=「回到程式預設」→ 從 .env 真移除+os.environ 拔除,
  絕不落 KEY='';非法數值照舊拒收不落檔。
- config:所有數值 env 讀取(_env_int/_env_float)容錯——「設了但留空」或垃圾值退回
  預設,reload 不拋。config.py 不得再出現裸 int(os.getenv(/float(os.getenv( 寫法。
- secretfile.remove_secret_key:移除 key、保 0600;檔案/鍵不存在=no-op。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from studio import config, secretfile, settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("TI_AUTOPILOT_INVESTIGATION_TIMEOUT=900\n", encoding="utf-8")
    monkeypatch.setattr(settings, "env_path", lambda: str(path))
    monkeypatch.setattr(settings.config, "reload", lambda: None)
    return path


def test_update_empty_numeric_unsets_key(env_file, monkeypatch):
    monkeypatch.setenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT", "900")
    settings.update({"TI_AUTOPILOT_INVESTIGATION_TIMEOUT": ""})
    text = env_file.read_text(encoding="utf-8")
    assert "TI_AUTOPILOT_INVESTIGATION_TIMEOUT" not in text, "空數字欄必須真移除,不得留 KEY=''"
    assert os.getenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT") is None, "行程環境也要拔除"


def test_update_bad_numeric_still_rejected(env_file, monkeypatch):
    monkeypatch.setenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT", "900")
    settings.update({"TI_AUTOPILOT_INVESTIGATION_TIMEOUT": "abc"})
    assert "=900" in env_file.read_text(encoding="utf-8"), "非法數值不落檔、不影響既有值"
    assert os.getenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT") == "900"


def test_update_valid_numeric_written(env_file, monkeypatch):
    settings.update({"TI_AUTOPILOT_INVESTIGATION_TIMEOUT": "600"})
    assert "600" in env_file.read_text(encoding="utf-8")
    assert os.getenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT") == "600"


def test_config_reload_tolerates_empty_and_garbage(monkeypatch):
    monkeypatch.setenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT", "")
    monkeypatch.setenv("TI_AUTOPILOT_FOLLOWUP_MAX_PER_TASK", "xyz")
    monkeypatch.setenv("TI_SESSION_SOFT_DEADLINE_FRAC", "")
    config.reload()  # 不得拋——2026-07-19 前這裡 int('') 直接 500/crashloop
    assert config.AUTOPILOT_INVESTIGATION_TIMEOUT == 1200, "空值退回預設"
    assert config.AUTOPILOT_FOLLOWUP_MAX_PER_TASK == 3, "垃圾值退回預設"


def test_config_no_bare_numeric_getenv():
    src = (ROOT / "studio" / "config.py").read_text(encoding="utf-8")
    assert "int(os.getenv(" not in src, "config.py 禁裸 int(os.getenv(——一律走 _env_int"
    assert "float(os.getenv(" not in src, "config.py 禁裸 float(os.getenv(——一律走 _env_float"


def test_remove_secret_key(tmp_path):
    path = tmp_path / ".env"
    secretfile.write_secret_file(str(path), "A", "1")
    secretfile.write_secret_file(str(path), "B", "2")
    secretfile.remove_secret_key(str(path), "A")
    text = path.read_text(encoding="utf-8")
    assert "A=" not in text and "B" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    secretfile.remove_secret_key(str(path), "NOPE")  # 不存在的鍵 no-op
    secretfile.remove_secret_key(str(tmp_path / "ghost.env"), "A")  # 檔案不存在 no-op
