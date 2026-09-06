from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from studio import providers


def _make_fake_openai_module(
    *, create_exc: Exception | None = None, close_exc: Exception | None = None
):
    captured: dict = {"clients": []}

    async def _create(**kwargs):
        captured["create_kwargs"] = kwargs
        if create_exc is not None:
            raise create_exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))]
        )

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.close_calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))
            captured["clients"].append(self)

        async def close(self):
            self.close_calls += 1
            if close_exc is not None:
                raise close_exc

    return SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI), captured


@pytest.mark.asyncio
async def test_openai_chat_closes_client_after_success(monkeypatch):
    module, captured = _make_fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", module)

    response = await providers._openai_chat(
        [{"role": "user", "content": "hi"}], None, "gpt-x", provider="openai"
    )

    assert response.choices[0].message.content == "ok"
    assert captured["clients"][0].close_calls == 1


@pytest.mark.asyncio
async def test_openai_chat_closes_client_when_create_raises(monkeypatch):
    boom = RuntimeError("api failed")
    module, captured = _make_fake_openai_module(create_exc=boom)
    monkeypatch.setitem(sys.modules, "openai", module)

    with pytest.raises(RuntimeError, match="api failed"):
        await providers._openai_chat(
            [{"role": "user", "content": "hi"}], None, "gpt-x", provider="openai"
        )

    assert captured["clients"][0].close_calls == 1


@pytest.mark.asyncio
async def test_openai_client_close_error_does_not_mask_response(monkeypatch):
    module, captured = _make_fake_openai_module(close_exc=RuntimeError("close failed"))
    monkeypatch.setitem(sys.modules, "openai", module)

    response = await providers._openai_chat(
        [{"role": "user", "content": "hi"}], None, "gpt-x", provider="openai"
    )

    assert response.choices[0].message.content == "ok"
    assert captured["clients"][0].close_calls == 1


@pytest.mark.asyncio
async def test_openai_chat_closes_client_when_close_is_sync(monkeypatch):
    captured: dict = {"clients": []}

    async def _create(**_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))]
        )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.close_calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))
            captured["clients"].append(self)

        def close(self):
            self.close_calls += 1

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))

    response = await providers._openai_chat(
        [{"role": "user", "content": "hi"}], None, "gpt-x", provider="openai"
    )

    assert response.choices[0].message.content == "ok"
    assert captured["clients"][0].close_calls == 1
