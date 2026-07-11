"""Claude prompt caching env 傳遞守門。"""

from __future__ import annotations

import sys
import types

from studio import config, experts
from studio.roles import BY_KEY


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


def test_prompt_cache_options_enabled(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_1H", True)
    assert experts._prompt_cache_options() == {"env": {"ENABLE_PROMPT_CACHING_1H": "1"}}


def test_build_client_passes_prompt_cache_env_when_enabled(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(config, "PROMPT_CACHE_1H", True)

    client = experts._build_client(BY_KEY["engineer"], "sid", tmp_path)

    assert client.options.env == {"ENABLE_PROMPT_CACHING_1H": "1"}


def test_build_client_omits_env_when_prompt_cache_disabled(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(config, "PROMPT_CACHE_1H", False)

    client = experts._build_client(BY_KEY["engineer"], "sid", tmp_path)

    option_keys = vars(client.options)
    assert "env" not in option_keys, "關閉態不要傳 env key，避免覆蓋 SDK default {}"
    assert option_keys.get("env", {}) is not None, "禁止 env=None，SDK 會在 **options.env 時炸掉"


def test_config_reload_picks_up_prompt_cache_env(monkeypatch):
    monkeypatch.setenv("TI_PROMPT_CACHE_1H", "0")
    config.reload()
    try:
        assert config.PROMPT_CACHE_1H is False

        monkeypatch.setenv("TI_PROMPT_CACHE_1H", "1")
        config.reload()
        assert config.PROMPT_CACHE_1H is True
    finally:
        monkeypatch.delenv("TI_PROMPT_CACHE_1H", raising=False)
        config.reload()
