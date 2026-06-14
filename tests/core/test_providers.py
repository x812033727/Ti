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


# --- complete_once OpenAI 路徑測試（任務 #2/#3/#4）---------------------------


def _setup_openai(monkeypatch, *, ready=True, offline=False):
    """設定 config 讓 complete_once 走 openai 分支且 provider_ready() 可控。

    一律用 monkeypatch.setattr，測後自動還原，不污染後續測試。
    """
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OFFLINE_MODE", offline)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://local" if ready else "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")


@pytest.mark.asyncio
async def test_complete_once_openai_success(monkeypatch, tmp_path):
    """任務 #2：成功路徑回傳正確純文字，且僅呼叫 chat 一次。"""
    _setup_openai(monkeypatch)
    fake = FakeChat([_msg(content="反思結論", tool_calls=None)])
    monkeypatch.setattr(providers, "_openai_chat", fake)

    out = await providers.complete_once(
        "你是反思器", "請反思", session_id="s", cwd=tmp_path, timeout=1.0
    )

    assert out == "反思結論"
    assert len(fake.seen) == 1  # 純 content 首回合即收斂


@pytest.mark.asyncio
async def test_complete_once_guard_cwd_none(monkeypatch):
    """任務 #3①：cwd=None 直接回 "" 且不觸碰 _openai_chat。"""
    _setup_openai(monkeypatch)
    fake = FakeChat([_msg(content="不該被呼叫")])
    monkeypatch.setattr(providers, "_openai_chat", fake)

    out = await providers.complete_once("sys", "user", session_id="s", cwd=None, timeout=1.0)

    assert out == ""
    assert fake.seen == []


@pytest.mark.asyncio
async def test_complete_once_guard_offline(monkeypatch, tmp_path):
    """任務 #3②：OFFLINE_MODE=True 直接回 "" 且不觸碰 _openai_chat。"""
    _setup_openai(monkeypatch, offline=True)
    fake = FakeChat([_msg(content="不該被呼叫")])
    monkeypatch.setattr(providers, "_openai_chat", fake)

    out = await providers.complete_once("sys", "user", session_id="s", cwd=tmp_path, timeout=1.0)

    assert out == ""
    assert fake.seen == []


@pytest.mark.asyncio
async def test_complete_once_guard_provider_not_ready(monkeypatch, tmp_path):
    """任務 #3③：provider_ready()=False（PROVIDER=openai 但金鑰/URL 皆空）回 "" 不觸碰 chat。"""
    _setup_openai(monkeypatch, ready=False)
    assert config.provider_ready() is False  # 自證守門條件成立
    fake = FakeChat([_msg(content="不該被呼叫")])
    monkeypatch.setattr(providers, "_openai_chat", fake)

    out = await providers.complete_once("sys", "user", session_id="s", cwd=tmp_path, timeout=1.0)

    assert out == ""
    assert fake.seen == []


@pytest.mark.asyncio
async def test_complete_once_openai_exception_degrades(monkeypatch, tmp_path):
    """任務 #4：_openai_chat 拋例外時回 "" 且不外拋（驗 except Exception: return ""）。"""
    _setup_openai(monkeypatch)

    called = {"n": 0}

    async def exploding_chat(messages, tools_, model):
        called["n"] += 1
        raise RuntimeError("API 炸了")

    monkeypatch.setattr(providers, "_openai_chat", exploding_chat)

    out = await providers.complete_once("sys", "user", session_id="s", cwd=tmp_path, timeout=1.0)

    assert out == ""
    # 反向對照：確實有走到 openai 路徑並觸發 chat（否則是 guard 短路的假綠）
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_complete_once_openai_chat_non_runtime_exception_also_degrades(monkeypatch, tmp_path):
    """邊界：驗 except Exception 為廣捕——非 RuntimeError（此處 ValueError）也降級回 "" 不外拋。"""
    _setup_openai(monkeypatch)

    called = {"n": 0}

    async def exploding_chat(messages, tools_, model):
        called["n"] += 1
        raise ValueError("非預期型別")

    monkeypatch.setattr(providers, "_openai_chat", exploding_chat)

    out = await providers.complete_once(
        "你是反思器", "請反思", session_id="s", cwd=tmp_path, timeout=1.0
    )

    assert out == ""
    assert called["n"] == 1
