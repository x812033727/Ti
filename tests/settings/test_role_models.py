"""每角色模型設定：欄位白名單、auto 語義、reload 即時生效、_model_for 優先序。"""

from __future__ import annotations

import os

import pytest

from studio import config, experts, settings
from studio.roles import BY_KEY

ROLE_ENVS = [f"TI_MODEL_{k.upper()}" for k in config.ROLE_KEYS]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """同 test_settings.py：.env 導向暫存目錄，測後還原環境與 config。"""
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


# ---------- 欄位形狀 ----------


def test_all_roles_have_select_field_with_recommended():
    fields = {f.env: f for f in settings.FIELDS}
    for env in ROLE_ENVS:
        f = fields[env]
        assert f.kind == "select" and f.group == "Claude"
        assert f.options[0] == "auto" and f.default == "auto"
        assert set(settings.CLAUDE_MODELS) <= set(f.options)
        assert f.recommended == "claude-fable-5"  # 品質優先推薦


def test_read_exposes_recommended(sandbox):
    fields = {f["env"]: f for f in settings.read()["fields"]}
    assert fields["TI_MODEL_ENGINEER"]["recommended"] == "claude-fable-5"
    assert fields["TI_MODEL_ENGINEER"]["value"] == "auto"  # 未設定時顯示有效預設


# ---------- update 驗證與 reload ----------


def test_update_accepts_model_and_auto_and_reloads(sandbox):
    settings.update({"TI_MODEL_ENGINEER": "claude-sonnet-4-6"})
    assert config.ROLE_MODELS["engineer"] == "claude-sonnet-4-6"
    settings.update({"TI_MODEL_ENGINEER": "auto"})
    assert config.ROLE_MODELS["engineer"] == ""  # auto＝不覆寫


def test_update_rejects_bad_role_model(sandbox, monkeypatch):
    monkeypatch.delenv("TI_MODEL_QA", raising=False)
    settings.update({"TI_MODEL_QA": "bogus-model"})
    assert os.environ.get("TI_MODEL_QA") != "bogus-model"
    assert config.ROLE_MODELS["qa"] == ""


# ---------- _model_for 優先序 ----------


def test_model_for_override_beats_lead_roles(monkeypatch):
    monkeypatch.setattr(config, "LEAD_ROLES", {"pm"})
    monkeypatch.setattr(config, "MODEL_LEAD", "lead-model")
    monkeypatch.setattr(config, "MODEL_FAST", "fast-model")
    monkeypatch.setattr(config, "ROLE_MODELS", {"engineer": "engineer-model"})
    assert experts._model_for(BY_KEY["engineer"]) == "engineer-model"
    # 沒覆寫的角色維持二分法
    assert experts._model_for(BY_KEY["pm"]) == "lead-model"
    assert experts._model_for(BY_KEY["qa"]) == "fast-model"


def test_model_for_auto_is_backward_compatible(monkeypatch):
    """全部 auto（空字串）＝與改動前完全相同的行為。"""
    monkeypatch.setattr(config, "LEAD_ROLES", {"pm"})
    monkeypatch.setattr(config, "MODEL_LEAD", "lead-model")
    monkeypatch.setattr(config, "MODEL_FAST", "fast-model")
    monkeypatch.setattr(config, "ROLE_MODELS", dict.fromkeys(config.ROLE_KEYS, ""))
    assert experts._model_for(BY_KEY["pm"]) == "lead-model"
    assert experts._model_for(BY_KEY["engineer"]) == "fast-model"
