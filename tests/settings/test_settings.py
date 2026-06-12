"""設定模組測試：讀取遮蔽秘密、更新只接受白名單、秘密留空不變更、reload 即時生效。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from studio import config, settings


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把 .env 導向暫存目錄，並在測試後還原被動到的環境變數與 config。"""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    keys = [f.env for f in settings.FIELDS]
    saved = {k: os.environ.get(k) for k in keys}
    yield tmp_path
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    config.reload()


def test_read_masks_secrets(sandbox, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-123")
    monkeypatch.setenv("TI_MODEL_LEAD", "claude-x")
    fields = {f["env"]: f for f in settings.read()["fields"]}
    # 秘密欄位不外洩明文，但回報 set=True
    assert fields["ANTHROPIC_API_KEY"]["value"] == ""
    assert fields["ANTHROPIC_API_KEY"]["set"] is True
    # 非秘密欄位回報實際值
    assert fields["TI_MODEL_LEAD"]["value"] == "claude-x"


def test_update_writes_and_reloads(sandbox):
    settings.update({"TI_PROVIDER": "openai", "TI_MODEL_LEAD": "claude-haiku-4-5"})
    assert config.PROVIDER == "openai"
    assert config.MODEL_LEAD == "claude-haiku-4-5"
    env_text = (sandbox / ".env").read_text()
    assert "TI_PROVIDER" in env_text and "openai" in env_text


def test_update_rejects_unknown_key(sandbox):
    settings.update({"EVIL_KEY": "x"})
    assert "EVIL_KEY" not in os.environ


def test_update_rejects_bad_select(sandbox):
    settings.update({"TI_PROVIDER": "bogus"})
    assert config.PROVIDER != "bogus"


def test_secret_blank_keeps_existing(sandbox, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_keep")
    settings.update({"GITHUB_TOKEN": ""})  # 留空＝不變更
    assert os.environ["GITHUB_TOKEN"] == "ghp_keep"


def test_settings_endpoints(sandbox, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用
    from studio.server import app

    # POST /api/settings 門禁停用時 fail-safe 限本機（require_admin）：以 loopback peer 連入。
    client = TestClient(app, client=("127.0.0.1", 12345))
    r = client.get("/api/settings")
    assert r.status_code == 200 and "fields" in r.json()
    r2 = client.post("/api/settings", json={"TI_MODEL_FAST": "claude-opus-4-7"})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    assert config.MODEL_FAST == "claude-opus-4-7"


def test_update_advanced_toggle_reloads(sandbox):
    """進階開關經設定面板存檔後即時生效（消費端讀即時全域值）。"""
    settings.update(
        {"TI_REFLEXION": "1", "TI_OBJECTIVE_GATE": "strict", "TI_SELF_REFINE_ITERS": "2"}
    )
    assert config.REFLEXION_ENABLED is True
    assert config.OBJECTIVE_GATE == "strict" and config.objective_gate_strict() is True
    assert config.SELF_REFINE_ITERS == 2
    # 關回去亦即時生效
    settings.update({"TI_REFLEXION": "0", "TI_OBJECTIVE_GATE": "0", "TI_SELF_REFINE_ITERS": "0"})
    assert config.REFLEXION_ENABLED is False
    assert config.objective_gate_enabled() is False
    assert config.SELF_REFINE_ITERS == 0


def test_update_rejects_bad_objective_gate(sandbox):
    """select 白名單：非法閘門值不被接受（維持原值）。"""
    settings.update({"TI_OBJECTIVE_GATE": "bogus"})
    assert config.OBJECTIVE_GATE != "bogus"


def test_read_shows_effective_default_for_unset_toggle(sandbox, monkeypatch):
    """env 未設定時，select 顯示「有效預設」（RLIMITS 預設開＝"1"）而非 raw 空字串。"""
    monkeypatch.delenv("TI_RLIMITS", raising=False)
    fields = {f["env"]: f for f in settings.read()["fields"]}
    assert fields["TI_RLIMITS"]["value"] == "1"  # 顯示預設「開」
    assert fields["TI_RLIMITS"]["set"] is False  # 但標記為未明確設定
