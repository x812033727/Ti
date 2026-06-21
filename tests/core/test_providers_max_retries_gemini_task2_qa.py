"""QA 驗證（任務 #2）：gemini 相容 provider 也以 max_retries=0 建構。

接縫選擇（架構決策）：_openai_chat 使用 lazy `import openai`，測試必須 patch
`sys.modules["openai"]` 注入假模組；patch `studio.providers.openai` 會打不到實際 import
路徑，容易假綠。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from studio import config, providers

_SDK_DEFAULT_MAX_RETRIES = 2


def _make_fake_openai_module():
    captured: dict = {}

    async def _create(**kwargs):
        captured["create_kwargs"] = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))]
        )

    class FakeAsyncOpenAI:
        def __init__(
            self, *, api_key=None, base_url=None, max_retries=_SDK_DEFAULT_MAX_RETRIES, **kw
        ):
            self.max_retries = max_retries
            captured.update(
                api_key=api_key,
                base_url=base_url,
                max_retries=max_retries,
                extra=kw,
            )
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))

    return SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI), captured


@pytest.fixture
def fake_openai(monkeypatch):
    module, captured = _make_fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", module)
    return module, captured


@pytest.mark.asyncio
async def test_gemini_client_built_with_max_retries_zero(fake_openai, monkeypatch):
    """gemini provider 路徑：AsyncOpenAI 收到 max_retries=0，且走 Gemini 憑證。"""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gm-key")
    monkeypatch.setattr(
        config, "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    _, captured = fake_openai

    await providers._openai_chat(
        [{"role": "user", "content": "hi"}], None, "gemini-2.5-flash", provider="gemini"
    )

    assert captured["max_retries"] == 0
    assert captured["max_retries"] != _SDK_DEFAULT_MAX_RETRIES
    assert captured["api_key"] == "gm-key"
    assert captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert captured["create_kwargs"]["model"] == "gemini-2.5-flash"


def test_fake_openai_defaults_to_sdk_retry_value_when_omitted(fake_openai):
    """反向黑樣本：若 providers 漏傳 max_retries，fake 會落回 SDK 預設 2。"""
    module, _ = fake_openai

    client = module.AsyncOpenAI(api_key="gm-key")

    assert client.max_retries == _SDK_DEFAULT_MAX_RETRIES
