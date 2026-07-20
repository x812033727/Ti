"""PM 釘選 provider／模型（TI_PM_PIN_PROVIDER / TI_PM_PIN_MODEL）的優先序與解除。

PM 是分派/檢驗/表決的最終決策者，判斷品質必須穩定：預設釘 claude + claude-fable-5，
優先於 per-role env 覆寫與全域 provider；設空字串＝解除釘選、回到一般優先序。
全程 stub，不打 LLM。
"""

from __future__ import annotations

from studio import config, providers
from studio.experts import _model_for
from studio.roles import BY_KEY

# --- provider 釘選 -----------------------------------------------------------


def test_pin_provider_beats_role_override_and_global(monkeypatch):
    monkeypatch.setattr(config, "PM_PIN_PROVIDER", "claude")
    monkeypatch.setattr(config, "PROVIDER", "codex")
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {"pm": "minimax"})
    assert providers.effective_provider(BY_KEY["pm"]) == "claude"  # 釘選 > TI_PROVIDER_PM > 全域
    # 其他角色不受釘選影響（engineer 無覆寫 → 全域）。
    assert providers.effective_provider(BY_KEY["engineer"]) == "codex"


def test_pin_provider_empty_releases(monkeypatch):
    monkeypatch.setattr(config, "PM_PIN_PROVIDER", "")
    monkeypatch.setattr(config, "PROVIDER", "codex")
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {"pm": "minimax"})
    assert providers.effective_provider(BY_KEY["pm"]) == "minimax"  # 解除 → per-role 覆寫
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {})
    assert providers.effective_provider(BY_KEY["pm"]) == "codex"  # 再無覆寫 → 全域


def test_pin_provider_invalid_value_treated_as_released(monkeypatch):
    monkeypatch.setattr(config, "PM_PIN_PROVIDER", "nosuch")  # 不在 config.PROVIDERS 白名單
    monkeypatch.setattr(config, "PROVIDER", "codex")
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {})
    assert providers.effective_provider(BY_KEY["pm"]) == "codex"


def test_default_pin_values_are_claude_fable():
    # conftest 已清空 TI_*，此處驗證的是 config 預設值本身。
    assert config.PM_PIN_PROVIDER == "claude"
    assert config.PM_PIN_MODEL == "claude-fable-5"


def test_make_expert_pm_pinned_to_claude_even_under_other_global(monkeypatch, tmp_path):
    """全域切 openai 時 PM 仍走 Claude Expert（釘選生效於 make_expert 分派）。"""
    from studio import experts

    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: object())
    monkeypatch.setattr(config, "PM_PIN_PROVIDER", "claude")
    monkeypatch.setattr(config, "PROVIDER", "openai")
    ex = providers.make_expert(BY_KEY["pm"], "t", tmp_path)
    assert isinstance(ex, experts.Expert)


# --- model 釘選 --------------------------------------------------------------


def test_pin_model_beats_role_models_and_lead(monkeypatch):
    monkeypatch.setattr(config, "PM_PIN_MODEL", "claude-fable-5")
    monkeypatch.setattr(config, "ROLE_MODELS", {"pm": "claude-opus-4-8"})
    monkeypatch.setattr(config, "MODEL_LEAD", "lead-model")
    assert _model_for(BY_KEY["pm"]) == "claude-fable-5"  # 釘選 > TI_MODEL_PM > LEAD 槽
    # 其他角色不受釘選影響。
    monkeypatch.setattr(config, "ROLE_MODELS", {})
    monkeypatch.setattr(config, "MODEL_FAST", "fast-model")
    assert _model_for(BY_KEY["engineer"]) == "fast-model"


def test_pin_model_empty_releases(monkeypatch):
    monkeypatch.setattr(config, "PM_PIN_MODEL", "")
    monkeypatch.setattr(config, "ROLE_MODELS", {"pm": "claude-opus-4-8"})
    assert _model_for(BY_KEY["pm"]) == "claude-opus-4-8"  # 解除 → per-role 模型覆寫
    monkeypatch.setattr(config, "ROLE_MODELS", {})
    monkeypatch.setattr(config, "MODEL_LEAD", "lead-model")
    assert _model_for(BY_KEY["pm"]) == "lead-model"  # 再無覆寫 → LEAD/FAST 二分


# --- config.reload() 接線 -----------------------------------------------------


def test_reload_reads_pin_env(monkeypatch):
    """TI_PM_PIN_* 走 config.reload()：UI 改 .env 後無需重啟即生效。"""
    monkeypatch.setenv("TI_PM_PIN_PROVIDER", "")
    monkeypatch.setenv("TI_PM_PIN_MODEL", "claude-opus-4-8")
    try:
        config.reload()
        assert config.PM_PIN_PROVIDER == ""
        assert config.PM_PIN_MODEL == "claude-opus-4-8"
    finally:
        monkeypatch.delenv("TI_PM_PIN_PROVIDER", raising=False)
        monkeypatch.delenv("TI_PM_PIN_MODEL", raising=False)
        config.reload()  # 還原預設，避免污染其他測試
