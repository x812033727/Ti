"""測試 OpenAI provider 的工具迴圈（以 fake chat 取代真實 API）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from studio import config, events, providers
from studio.roles import BY_KEY


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tc(id, name, arguments):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=arguments))


class FakeChat:
    def __init__(self, responses):
        self.responses = responses
        self.i = 0
        self.seen = []

    async def __call__(self, messages, tools, model):
        self.seen.append({"messages": list(messages), "tools": tools, "model": model})
        r = self.responses[self.i]
        self.i += 1
        return r


def collect():
    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


def test_openai_model_for():
    assert providers.openai_model_for(BY_KEY["pm"]) == config.OPENAI_MODEL_LEAD
    assert providers.openai_model_for(BY_KEY["engineer"]) == config.OPENAI_MODEL_FAST


@pytest.mark.asyncio
async def test_tool_loop_writes_file_then_answers(tmp_path):
    chat = FakeChat(
        [
            _msg(
                tool_calls=[_tc("c1", "write_file", '{"path": "main.py", "content": "print(1)"}')]
            ),
            _msg(content="已建立 main.py"),
        ]
    )
    expert = providers.OpenAIExpert(BY_KEY["engineer"], "t", tmp_path, chat=chat, model="m")
    bucket, broadcast = collect()

    out = await expert.speak("實作", broadcast)

    assert out == "已建立 main.py"
    assert (tmp_path / "main.py").read_text() == "print(1)"
    types = [e.type for e in bucket]
    assert events.EventType.TOOL_USE in types
    assert events.EventType.EXPERT_MESSAGE in types
    # 第二次呼叫時，歷史已包含 assistant(tool_calls) 與 tool 結果
    roles_in_history = [m["role"] for m in chat.seen[1]["messages"]]
    assert "tool" in roles_in_history and "assistant" in roles_in_history


@pytest.mark.asyncio
async def test_tool_loop_plain_answer(tmp_path):
    chat = FakeChat([_msg(content="決議: 核可")])
    expert = providers.OpenAIExpert(BY_KEY["senior"], "t", tmp_path, chat=chat, model="m")
    bucket, broadcast = collect()
    out = await expert.speak("審查", broadcast)
    assert out == "決議: 核可"


def test_make_expert_openai(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROVIDER", "openai")
    ex = providers.make_expert(BY_KEY["pm"], "t", tmp_path)
    assert isinstance(ex, providers.OpenAIExpert)


# --- 任務 #3：complete_once 短路守門三態 ---
# 三態各自回 ""，且裝 FakeChat spy 斷言 seen == [] 作反向對照（排假綠）：
# 守門生效時根本不會建立 expert、不觸碰 _openai_chat。


@pytest.mark.asyncio
async def test_complete_once_guard_cwd_none(monkeypatch, tmp_path):
    """① cwd=None：即使 provider 就緒也短路回 ""，不碰 _openai_chat。"""
    spy = FakeChat([_msg(content="不該被呼叫")])
    monkeypatch.setattr(providers, "_openai_chat", spy)
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://local")

    out = await providers.complete_once(
        "sys", "usr", session_id="t", cwd=None, timeout=1.0
    )

    assert out == ""
    assert spy.seen == []


@pytest.mark.asyncio
async def test_complete_once_guard_offline_mode(monkeypatch, tmp_path):
    """② OFFLINE_MODE=True：離線旗標短路回 ""，不碰 _openai_chat。"""
    spy = FakeChat([_msg(content="不該被呼叫")])
    monkeypatch.setattr(providers, "_openai_chat", spy)
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://local")

    out = await providers.complete_once(
        "sys", "usr", session_id="t", cwd=tmp_path, timeout=1.0
    )

    assert out == ""
    assert spy.seen == []


@pytest.mark.asyncio
async def test_complete_once_guard_provider_not_ready(monkeypatch, tmp_path):
    """③ provider_ready()=False：openai 分支下 API_KEY/BASE_URL 皆空 → 短路回 ""。

    必須同時設 PROVIDER="openai"，否則 provider_ready() 走 claude 分支，
    驗的就不是目標行為（見架構決策）。
    """
    spy = FakeChat([_msg(content="不該被呼叫")])
    monkeypatch.setattr(providers, "_openai_chat", spy)
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "")
    assert config.provider_ready() is False  # 前提自證

    out = await providers.complete_once(
        "sys", "usr", session_id="t", cwd=tmp_path, timeout=1.0
    )

    assert out == ""
    assert spy.seen == []
