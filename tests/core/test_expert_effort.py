"""per-role 推理深度旋鈕(效能強化 A2)。

守護不變量:
- effort_for 優先序:per-role map > 全域 > None;非法值略過。
- 兩旋鈕皆空(預設)→ None(零行為改變 oracle,_build_client 傳 effort=None=SDK 預設)。
- config.reload() 後 env 生效。
- _build_client 傳遞 effort(假 SDK 斷言);既有 `lambda role, sid, cwd` monkeypatch 簽名不破。
"""

from __future__ import annotations

import sys
import types

import pytest

from studio import config, experts
from studio.roles import BY_KEY


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_EFFORT", "")
    monkeypatch.setattr(config, "EXPERT_EFFORT_MAP", {})


def test_default_is_none_zero_behavior():
    assert config.effort_for("pm") is None
    assert config.effort_for("security") is None


def test_map_overrides_global(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_EFFORT", "high")
    monkeypatch.setattr(config, "EXPERT_EFFORT_MAP", {"security": "low"})
    assert config.effort_for("security") == "low", "per-role 覆寫優先"
    assert config.effort_for("pm") == "high", "無覆寫走全域"
    assert config.effort_for("SECURITY") == "low", "role key 大小寫不敏感"


def test_parse_map_skips_invalid():
    parsed = config._parse_effort_map("security:low, architect:MEDIUM, qa:turbo, :low, pm:")
    assert parsed == {"security": "low", "architect": "medium"}, "非法 level 與空 key/val 略過"


def test_invalid_global_returns_none(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_EFFORT", "turbo")
    assert config.effort_for("pm") is None, "非法全域值不得傳給 SDK"


def test_reload_picks_up_env(monkeypatch):
    monkeypatch.setenv("TI_EXPERT_EFFORT_MAP", "oneshot:low")
    config.reload()
    try:
        assert config.effort_for("oneshot") == "low"
    finally:
        monkeypatch.delenv("TI_EXPERT_EFFORT_MAP")
        config.reload()


def _install_fake_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class ClaudeSDKClient:
        def __init__(self, options=None):
            self.options = options

    class HookMatcher:
        def __init__(self, matcher=None, hooks=None, timeout=None):
            self.matcher = matcher
            self.hooks = hooks or []
            self.timeout = timeout

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ClaudeSDKClient = ClaudeSDKClient
    mod.HookMatcher = HookMatcher
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)


def test_build_client_passes_effort(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(config, "EXPERT_EFFORT_MAP", {"security": "low"})

    client = experts._build_client(BY_KEY["security"], "sid", tmp_path)
    assert client.options.effort == "low"

    client2 = experts._build_client(BY_KEY["pm"], "sid", tmp_path)
    assert client2.options.effort is None, "未設角色必須是 None(SDK 預設)"
