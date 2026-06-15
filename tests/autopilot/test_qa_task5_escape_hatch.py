"""QA 驗收：任務 #5「逃生開關 require_chown=false（須顯式設定）+ warning + 文件 breaking change」。

對應 task #5 驗收標準：
  - 顯式設 require_chown=false 時：放行（不 raise），但輸出 warning log。
  - 逃生開關須「顯式」設定：未設定（預設）時為 strict，逃生開關不會自動觸發、不記降級 warning。
  - 文件/migration note 標明此預設變更為 breaking change。

設計補充：false→off。off 在每次寫入是「靜默放行」，但啟動（config 載入）時對任何非
strict 值記一條明顯 warning，以取代逐次寫入洗版——故「顯式 false → warning」在 config
載入層驗證。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from studio import config
from studio.secure_write import secure_write_root

README = Path(__file__).resolve().parents[2] / "README.md"


def _reload_config_capturing(monkeypatch, caplog, value):
    """設定 TI_REQUIRE_CHOWN 後重載 config，回傳 (有效模式, 該次載入的 WARNING 記錄)。"""
    if value is None:
        monkeypatch.delenv("TI_REQUIRE_CHOWN", raising=False)
    else:
        monkeypatch.setenv("TI_REQUIRE_CHOWN", value)
    caplog.clear()
    with caplog.at_level("WARNING", logger="studio.config"):
        importlib.reload(config)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    return config.REQUIRE_CHOWN, warnings


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    importlib.reload(config)


# --- 逃生開關須「顯式」：未設定時為 strict，不觸發、不記降級 warning -----
def test_unset_is_strict_no_escape_warning(monkeypatch, caplog):
    mode, warnings = _reload_config_capturing(monkeypatch, caplog, None)
    assert mode == "strict"
    assert warnings == []  # 未顯式降級 → 不應有 warning


def test_strict_explicit_no_escape_warning(monkeypatch, caplog):
    mode, warnings = _reload_config_capturing(monkeypatch, caplog, "strict")
    assert mode == "strict"
    assert warnings == []


# --- 顯式 false → 放行(off) 且 config 載入記 warning --------------------
def test_false_aliases_to_off(monkeypatch, caplog):
    mode, _ = _reload_config_capturing(monkeypatch, caplog, "false")
    assert mode == "off"


def test_explicit_false_emits_warning_at_load(monkeypatch, caplog):
    mode, warnings = _reload_config_capturing(monkeypatch, caplog, "false")
    assert mode == "off"
    assert len(warnings) >= 1
    blob = " ".join(r.message + str(r.args) for r in warnings)
    assert "TI_REQUIRE_CHOWN" in blob  # warning 指名旗標
    # 提到「降級／放寬」語意，提醒安全保證被放寬
    assert any(k in blob for k in ("降級", "放寬", "放行"))


@pytest.mark.parametrize("val", ["off", "0", "no"])
def test_off_synonyms_warn_at_load(monkeypatch, caplog, val):
    mode, warnings = _reload_config_capturing(monkeypatch, caplog, val)
    assert mode == "off"
    assert len(warnings) >= 1


def test_warn_mode_also_warns_at_load(monkeypatch, caplog):
    mode, warnings = _reload_config_capturing(monkeypatch, caplog, "warn")
    assert mode == "warn"
    assert len(warnings) >= 1


# --- off 在寫入層：放行、不 raise（即使 chown 會失敗）------------------
def test_off_write_passes_even_if_chown_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "fchown", lambda *a: (_ for _ in ()).throw(PermissionError("EPERM")))
    target = tmp_path / "state"
    # off 不應 raise，且應寫出檔案（逃生開關＝放行）
    secure_write_root(target, b"data", require_chown="off")
    assert target.exists()
    assert target.read_bytes() == b"data"


# --- 文件/migration note：標明 breaking change + 遷移指引 ---------------
def test_readme_documents_breaking_change():
    text = README.read_text(encoding="utf-8")
    assert "TI_REQUIRE_CHOWN" in text
    assert "Breaking change" in text or "breaking change" in text
    # 遷移指引：非 root 部署應顯式設 warn 過渡
    assert "TI_REQUIRE_CHOWN=warn" in text
    # 逃生開關 off 也應被文件提及
    assert "TI_REQUIRE_CHOWN=off" in text or "`off`" in text


def test_readme_states_default_is_strict():
    text = README.read_text(encoding="utf-8")
    # 預設嚴格須在文件明確標示
    assert "strict" in text
    assert "預設" in text
