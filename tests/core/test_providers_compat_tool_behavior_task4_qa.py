"""任務 #4：OpenAI 相容後端工具行為防護。

本檔補兩個相容後端常見風險，皆含反向黑樣本對照：
- malformed tool_call 不能炸掉 speak；若同則訊息有 content，回退一般 content。
- gemini/minimax 等 OpenAI 相容路徑 retry 重放同位置非冪等工具時，命中 DedupCache 不重跑副作用。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from studio import config, experts, providers
from studio.roles import BY_KEY


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tc(tool_id, name, arguments):
    return SimpleNamespace(id=tool_id, function=SimpleNamespace(name=name, arguments=arguments))


class ScriptedChat:
    def __init__(self, actions):
        self.actions = actions
        self.calls = 0

    async def __call__(self, messages, tools, model):
        idx = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        action = self.actions[idx]
        if isinstance(action, BaseException):
            raise action
        return action


async def _noop_broadcast(ev):
    return None


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(experts, "_sleep", fake_sleep)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 2)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)


def _expert(chat, tmp_path, provider="gemini"):
    return providers.OpenAIExpert(
        BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m", provider=provider
    )


def _rate_limit_err():
    return RuntimeError("Error code: 429 - Rate limit reached")


@pytest.mark.asyncio
async def test_malformed_tool_call_falls_back_to_content(tmp_path):
    """壞 JSON tool_call 代表工具解析失敗；有 content 時應回一般文字，不執行工具。"""
    chat = ScriptedChat(
        [_msg(content="直接用文字回答", tool_calls=[_tc("bad", "run_bash", "{not json")])]
    )

    out = await _expert(chat, tmp_path, provider="gemini").speak("做事", _noop_broadcast)

    assert out == "直接用文字回答"
    assert chat.calls == 1
    assert not (tmp_path / "log.txt").exists()


@pytest.mark.asyncio
async def test_BLACK_valid_tool_call_with_content_still_enters_tool_loop(tmp_path):
    """反向對照：格式正確的 tool_call 不可被 content 吞掉，必須實際跑工具迴圈。"""
    args_json = json.dumps({"command": "echo ran >> log.txt"})
    chat = ScriptedChat(
        [
            _msg(content="不要直接回這句", tool_calls=[_tc("ok", "run_bash", args_json)]),
            _msg(content="工具後結論"),
        ]
    )

    out = await _expert(chat, tmp_path, provider="gemini").speak("做事", _noop_broadcast)

    assert out == "工具後結論"
    assert chat.calls == 2
    assert (tmp_path / "log.txt").read_text().splitlines() == ["ran"]


@pytest.mark.parametrize("provider", ["openai", "minimax", "gemini"])
@pytest.mark.asyncio
async def test_compat_provider_retry_replay_dedups_non_idempotent_tool(
    provider, tmp_path
):
    """同 args 的 run_bash 在 retry 重放同位置時只執行一次，覆蓋三個 OpenAI 相容 provider。"""
    args_json = json.dumps({"command": "echo once >> replay.txt"})
    chat = ScriptedChat(
        [
            _msg(tool_calls=[_tc("c1", "run_bash", args_json)]),
            _rate_limit_err(),
            _msg(tool_calls=[_tc("c2", "run_bash", args_json)]),
            _msg(content="完成"),
        ]
    )

    out = await _expert(chat, tmp_path, provider=provider).speak("做事", _noop_broadcast)

    assert out == "完成"
    assert chat.calls == 4
    assert (tmp_path / "replay.txt").read_text().splitlines() == ["once"]


@pytest.mark.parametrize("provider", ["openai", "minimax", "gemini"])
@pytest.mark.asyncio
async def test_BLACK_compat_provider_retry_replay_changed_args_is_not_deduped(
    provider, tmp_path
):
    """反向對照：retry 重放時 args 改變就不可誤命中去重，副作用會跑兩次。"""
    first = json.dumps({"command": "echo twice >> drift.txt"})
    drifted = json.dumps({"command": "echo twice  >> drift.txt"})
    chat = ScriptedChat(
        [
            _msg(tool_calls=[_tc("c1", "run_bash", first)]),
            _rate_limit_err(),
            _msg(tool_calls=[_tc("c2", "run_bash", drifted)]),
            _msg(content="完成"),
        ]
    )

    out = await _expert(chat, tmp_path, provider=provider).speak("做事", _noop_broadcast)

    assert out == "完成"
    assert chat.calls == 4
    assert (tmp_path / "drift.txt").read_text().splitlines() == ["twice", "twice"]
