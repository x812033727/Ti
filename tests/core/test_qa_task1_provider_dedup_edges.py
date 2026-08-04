"""QA：OpenAI-compatible speak retry 去重的邊界驗證。

這裡補工程師測試較少碰到的兩個失敗路徑：
- 同一工具 args JSON 鍵序不同，仍必須命中同一去重 key；
- retry 後 args 內容真的不同，不可被錯誤地當成同一次副作用吞掉。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from studio import config, experts, providers
from studio.roles import BY_KEY


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=None,
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


async def _broadcast(_ev):
    return None


@pytest.fixture(autouse=True)
def _retry_without_wait(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(experts, "_sleep", fake_sleep)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 3)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)


def _expert(chat, tmp_path):
    return providers.OpenAIExpert(
        BY_KEY["engineer"], "qa-session", tmp_path, chat=chat, model="qa-model"
    )


def _rate_limit_err():
    return RuntimeError("Error code: 429 - Rate limit reached")


@pytest.mark.asyncio
async def test_retry_dedup_ignores_json_key_order_for_same_run_bash(tmp_path):
    command = "printf 'once\\n' >> ordered.txt"
    first_args = json.dumps({"command": command, "unused": "same"})
    replay_args = json.dumps({"unused": "same", "command": command})
    chat = ScriptedChat(
        [
            _msg(tool_calls=[_tc("call_original", "run_bash", first_args)]),
            _rate_limit_err(),
            _msg(tool_calls=[_tc("call_replay", "run_bash", replay_args)]),
            _msg(content="完成"),
        ]
    )

    out = await _expert(chat, tmp_path).speak("請執行", _broadcast)

    assert out == "完成"
    assert chat.calls == 4
    assert (tmp_path / "ordered.txt").read_text().splitlines() == ["once"]


@pytest.mark.asyncio
async def test_retry_dedup_does_not_swallow_changed_run_bash_args(tmp_path):
    first_args = json.dumps({"command": "printf 'first\\n' >> changed.txt"})
    changed_args = json.dumps({"command": "printf 'second\\n' >> changed.txt"})
    chat = ScriptedChat(
        [
            _msg(tool_calls=[_tc("call_original", "run_bash", first_args)]),
            _rate_limit_err(),
            _msg(tool_calls=[_tc("call_changed", "run_bash", changed_args)]),
            _msg(content="完成"),
        ]
    )

    out = await _expert(chat, tmp_path).speak("請執行", _broadcast)

    assert out == "完成"
    assert chat.calls == 4
    assert (tmp_path / "changed.txt").read_text().splitlines() == ["first", "second"]
