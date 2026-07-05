"""_model_for 末段的 scoped 週限自動改派（Fable 滿→Opus 備援）。

補額度閘門盲點：provider_quota 只看全域 5h/7d，刻意不含 model-scoped 週限（「Fable 滿≠
claude 受限」）。代價是——當專家被釘在 Fable、而 Fable 週限滿載時，閘門仍判 claude 可用，
每次呼叫秒失敗、任務退回 pending 空轉到週限重置。_reroute_if_scoped_exhausted 在建 session
（LLM 呼叫前）把撞滿 scoped 的模型換成非 scoped 備援。全程 monkeypatch fetch_rate_limits，
不打網路、不碰真 token。
"""

from __future__ import annotations

import pytest

from studio import claude_usage, config
from studio.experts import _model_for, _reroute_if_scoped_exhausted
from studio.roles import BY_KEY


def _fake_usage(monkeypatch, models=None, error=None):
    monkeypatch.setattr(
        claude_usage,
        "fetch_rate_limits",
        lambda *a, **k: {"models": models, "error": error},
    )


@pytest.fixture(autouse=True)
def _fixed_scoped_config(monkeypatch):
    # 固定門檻與備援，測試不受 env/預設漂移影響。
    monkeypatch.setattr(config, "CLAUDE_SCOPED_FALLBACK_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(config, "CLAUDE_SCOPED_LIMIT_THRESHOLD", 95.0)


def test_fable_exhausted_reroutes_to_opus(monkeypatch):
    _fake_usage(monkeypatch, models={"Fable": {"used_percentage": 100.0}})
    assert _reroute_if_scoped_exhausted("claude-fable-5") == "claude-opus-4-8"


def test_fable_below_threshold_keeps_model(monkeypatch):
    _fake_usage(monkeypatch, models={"Fable": {"used_percentage": 80.0}})
    assert _reroute_if_scoped_exhausted("claude-fable-5") == "claude-fable-5"


def test_no_scoped_data_keeps_model(monkeypatch):
    _fake_usage(monkeypatch, models=None)
    assert _reroute_if_scoped_exhausted("claude-fable-5") == "claude-fable-5"


def test_query_error_keeps_model(monkeypatch):
    # 額度查詢異常（unauthorized/unreachable）→ 保守不改派，維持原模型。
    _fake_usage(
        monkeypatch,
        models={"Fable": {"used_percentage": 100.0}},
        error="unauthorized",
    )
    assert _reroute_if_scoped_exhausted("claude-fable-5") == "claude-fable-5"


def test_fallback_itself_exhausted_keeps_model(monkeypatch):
    # 備援（Opus）也撞 scoped 週限 → 改派無益，維持原模型交回額度閘門/帳號輪替。
    _fake_usage(
        monkeypatch,
        models={
            "Fable": {"used_percentage": 100.0},
            "Opus": {"used_percentage": 97.0},
        },
    )
    assert _reroute_if_scoped_exhausted("claude-fable-5") == "claude-fable-5"


def test_non_claude_model_untouched(monkeypatch):
    # display_name「Fable」不出現在非 claude 模型 id 內 → 不誤傷。
    _fake_usage(monkeypatch, models={"Fable": {"used_percentage": 100.0}})
    assert _reroute_if_scoped_exhausted("gpt-4o") == "gpt-4o"


def test_disabled_when_no_fallback(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_SCOPED_FALLBACK_MODEL", "")
    _fake_usage(monkeypatch, models={"Fable": {"used_percentage": 100.0}})
    assert _reroute_if_scoped_exhausted("claude-fable-5") == "claude-fable-5"


def test_model_equal_to_fallback_no_reroute(monkeypatch):
    # 已是備援模型本身：即便查得到，也不再改派（避免自我改派/無窮遞迴）。
    _fake_usage(monkeypatch, models={"Opus": {"used_percentage": 100.0}})
    assert _reroute_if_scoped_exhausted("claude-opus-4-8") == "claude-opus-4-8"


def test_model_for_pm_pinned_fable_reroutes(monkeypatch):
    # 端到端：PM 釘 Fable + Fable 週限滿 → _model_for 直接回備援（本 incident 的修復）。
    monkeypatch.setattr(config, "PM_PIN_MODEL", "claude-fable-5")
    _fake_usage(monkeypatch, models={"Fable": {"used_percentage": 100.0}})
    assert _model_for(BY_KEY["pm"]) == "claude-opus-4-8"


def test_reload_reads_scoped_env(monkeypatch):
    """TI_CLAUDE_SCOPED_* 走 config.reload()：UI 改 .env 後無需重啟即生效。"""
    monkeypatch.setenv("TI_CLAUDE_SCOPED_FALLBACK_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("TI_CLAUDE_SCOPED_LIMIT_THRESHOLD", "88")
    try:
        config.reload()
        assert config.CLAUDE_SCOPED_FALLBACK_MODEL == "claude-sonnet-5"
        assert config.CLAUDE_SCOPED_LIMIT_THRESHOLD == 88.0
    finally:
        monkeypatch.delenv("TI_CLAUDE_SCOPED_FALLBACK_MODEL", raising=False)
        monkeypatch.delenv("TI_CLAUDE_SCOPED_LIMIT_THRESHOLD", raising=False)
        config.reload()  # 還原預設，避免污染其他測試
