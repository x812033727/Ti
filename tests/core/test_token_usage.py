"""Token 用量記錄與統計：events / providers(OpenAI) / experts(Claude) / history 聚合 / report。"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from studio import events, experts, history, providers, usage_report
from studio.events import EventType
from studio.roles import BY_KEY


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


async def _agen(items):
    for it in items:
        yield it


def _usage(p, c, t):
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c, total_tokens=t)


def _resp(content=None, tool_calls=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=usage,
    )


def _tc(id, name, arguments):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=arguments))


# --- events 建構器 -------------------------------------------------------


def test_token_usage_event_shape():
    ev = events.token_usage("s", "engineer", "minimax", "MiniMax-M3", 10, 5, 15, cost_usd=0.1)
    assert ev.type == EventType.TOKEN_USAGE
    d = ev.to_dict()
    assert d["type"] == "token_usage"
    p = d["payload"]
    assert (p["prompt_tokens"], p["completion_tokens"], p["total_tokens"]) == (10, 5, 15)
    assert p["provider"] == "minimax" and p["model"] == "MiniMax-M3" and p["cost_usd"] == 0.1


# --- history._derive_token_usage 聚合 ------------------------------------


def test_derive_token_usage_aggregates_by_group():
    evs = [
        {"type": "expert_message", "payload": {"text": "hi"}},  # 非 token_usage 應忽略
        {
            "type": "token_usage",
            "payload": {
                "speaker": "engineer", "provider": "minimax", "model": "MiniMax-M3",
                "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost_usd": None,
            },
        },
        {
            "type": "token_usage",
            "payload": {
                "speaker": "pm", "provider": "claude", "model": "claude-opus-4-8",
                "prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250, "cost_usd": 0.5,
            },
        },
        {
            "type": "token_usage",
            "payload": {
                "speaker": "engineer", "provider": "minimax", "model": "MiniMax-M3",
                "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "cost_usd": None,
            },
        },
    ]
    tu = history._derive_token_usage(evs)
    assert tu["total"] == {
        "prompt": 310, "completion": 74, "total": 384, "cost_usd": 0.5, "calls": 3
    }
    assert tu["by_provider"]["minimax"]["total"] == 134
    assert tu["by_provider"]["minimax"]["calls"] == 2
    assert tu["by_provider"]["claude"]["cost_usd"] == 0.5
    assert tu["by_model"]["MiniMax-M3"]["prompt"] == 110
    assert tu["by_role"]["engineer"]["total"] == 134
    assert tu["by_role"]["pm"]["completion"] == 50


def test_derive_token_usage_empty():
    tu = history._derive_token_usage([{"type": "done", "payload": {}}])
    assert tu["total"]["calls"] == 0
    assert tu["by_provider"] == {}


# --- experts.stream_to_events（Claude 路徑）------------------------------


@pytest.fixture
def fake_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, usage=None, total_cost_usd=None):
            self.usage = usage
            self.total_cost_usd = total_cost_usd

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ToolUseBlock:
        def __init__(self, name, input):
            self.name = name
            self.input = input

    mod.AssistantMessage = AssistantMessage
    mod.ResultMessage = ResultMessage
    mod.TextBlock = TextBlock
    mod.ToolUseBlock = ToolUseBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


@pytest.mark.asyncio
async def test_stream_to_events_emits_token_usage(fake_sdk):
    role = BY_KEY["pm"]
    msgs = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("結論")]),
        fake_sdk.ResultMessage(
            usage={
                "input_tokens": 1000, "output_tokens": 200,
                "cache_read_input_tokens": 800, "cache_creation_input_tokens": 50,
            },
            total_cost_usd=0.012,
        ),
    ]
    bucket, broadcast = collect()
    await experts.stream_to_events(_agen(msgs), "s", role, broadcast)

    tu = [e for e in bucket if e.type == EventType.TOKEN_USAGE]
    assert len(tu) == 1
    p = tu[0].payload
    assert p["provider"] == "claude"
    assert (p["prompt_tokens"], p["completion_tokens"], p["total_tokens"]) == (1000, 200, 1200)
    assert p["cost_usd"] == 0.012
    assert p["cache_read"] == 800 and p["cache_write"] == 50


@pytest.mark.asyncio
async def test_stream_to_events_no_usage_no_event(fake_sdk):
    """ResultMessage 無 usage（如既有測試的裸 ResultMessage）不得 emit、不得拋錯。"""
    role = BY_KEY["pm"]
    msgs = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("前")]),
        fake_sdk.ResultMessage(),  # usage=None, cost=None
    ]
    bucket, broadcast = collect()
    text = await experts.stream_to_events(_agen(msgs), "s", role, broadcast)
    assert text == "前"
    assert not [e for e in bucket if e.type == EventType.TOKEN_USAGE]


# --- providers.OpenAIExpert（OpenAI/MiniMax 路徑）------------------------


class _SeqChat:
    """依序回傳 responses；元素為 Exception 則於該次呼叫 raise。"""

    def __init__(self, responses):
        self.responses = responses
        self.i = 0

    async def __call__(self, messages, tools, model, **_kw):
        r = self.responses[self.i]
        self.i += 1
        if isinstance(r, Exception):
            raise r
        return r


@pytest.mark.asyncio
async def test_speak_sums_usage_over_tool_loop_emits_once(tmp_path):
    chat = _SeqChat([
        _resp(tool_calls=[_tc("1", "read_file", '{"path": "a.py"}')], usage=_usage(100, 0, 100)),
        _resp(content="完成", usage=_usage(30, 20, 50)),
    ])
    expert = providers.OpenAIExpert(BY_KEY["engineer"], "t", tmp_path, chat=chat, model="MiniMax-M3")
    bucket, broadcast = collect()
    await expert.speak("做事", broadcast)

    tu = [e for e in bucket if e.type == EventType.TOKEN_USAGE]
    assert len(tu) == 1
    p = tu[0].payload
    # 兩輪累加：100+30 / 0+20 / 100+50
    assert (p["prompt_tokens"], p["completion_tokens"], p["total_tokens"]) == (130, 20, 150)
    assert p["model"] == "MiniMax-M3"


@pytest.mark.asyncio
async def test_speak_without_usage_no_event(tmp_path):
    chat = _SeqChat([_resp(content="答")])  # usage=None
    expert = providers.OpenAIExpert(BY_KEY["senior"], "t", tmp_path, chat=chat, model="m")
    bucket, broadcast = collect()
    await expert.speak("審查", broadcast)
    assert not [e for e in bucket if e.type == EventType.TOKEN_USAGE]


@pytest.mark.asyncio
async def test_speak_retry_does_not_double_count(tmp_path, monkeypatch):
    """attempt 重放時 usage_acc 歸零：只計最終成功 attempt，不疊加被重試掉的那輪。"""
    monkeypatch.setattr(experts, "_sleep", lambda *_a, **_k: _noop())
    monkeypatch.setattr(experts.config, "EXPERT_RATE_LIMIT_RETRIES", 2)
    rl = experts.llm_caller.RateLimitSignal(0.0, "429", "")
    chat = _SeqChat([
        # attempt 1：先累加 7，再撞限流 → 整個 _attempt 重放
        _resp(tool_calls=[_tc("1", "read_file", '{"path": "a"}')], usage=_usage(7, 0, 7)),
        rl,
        # attempt 2（重放）：直接成功，usage=10
        _resp(content="OK", usage=_usage(10, 0, 10)),
    ])
    expert = providers.OpenAIExpert(BY_KEY["engineer"], "t", tmp_path, chat=chat, model="m")
    bucket, broadcast = collect()
    out = await expert.speak("做", broadcast)
    assert out == "OK"
    tu = [e for e in bucket if e.type == EventType.TOKEN_USAGE]
    assert len(tu) == 1
    # 只計成功 attempt 的 10，不是 7+10=17
    assert tu[0].payload["total_tokens"] == 10


async def _noop():
    return None


# --- usage_report 跨 session 彙總 ---------------------------------------


def test_usage_report_aggregate_and_minimax_estimate(monkeypatch):
    metas = [
        {"session_id": "a", "started_at": 1000, "token_usage": {
            "total": {"prompt": 1_000_000, "completion": 500_000, "total": 1_500_000,
                      "cost_usd": 0.0, "calls": 1},
            "by_provider": {"minimax": {"prompt": 1_000_000, "completion": 500_000,
                                        "total": 1_500_000, "cost_usd": 0.0, "calls": 1}},
            "by_model": {"MiniMax-M3": {"prompt": 1_000_000, "completion": 500_000,
                                        "total": 1_500_000, "cost_usd": 0.0, "calls": 1}},
            "by_role": {"engineer": {"prompt": 1_000_000, "completion": 500_000,
                                     "total": 1_500_000, "cost_usd": 0.0, "calls": 1}},
        }},
        {"session_id": "b", "started_at": 2000, "token_usage": {
            "total": {"prompt": 100, "completion": 50, "total": 150, "cost_usd": 0.9, "calls": 1},
            "by_provider": {"claude": {"prompt": 100, "completion": 50, "total": 150,
                                       "cost_usd": 0.9, "calls": 1}},
            "by_model": {"claude-opus-4-8": {"prompt": 100, "completion": 50, "total": 150,
                                             "cost_usd": 0.9, "calls": 1}},
            "by_role": {"pm": {"prompt": 100, "completion": 50, "total": 150,
                               "cost_usd": 0.9, "calls": 1}},
        }},
    ]
    monkeypatch.setattr(usage_report.history, "list_sessions", lambda: metas)
    agg = usage_report.aggregate()
    assert agg["sessions"] == 2
    assert agg["total"]["total"] == 1_500_150
    assert agg["total"]["cost_usd"] == 0.9  # Claude SDK 成本
    # MiniMax 估算：1M input * $0.30/M + 0.5M output * $1.20/M = 0.30 + 0.60 = 0.90
    assert agg["est_extra_usd"] == pytest.approx(0.90)
    # --since 過濾掉較早的 session a
    agg2 = usage_report.aggregate(since=1500)
    assert agg2["sessions"] == 1 and agg2["total"]["total"] == 150


def test_usage_report_renders(monkeypatch):
    monkeypatch.setattr(usage_report.history, "list_sessions", lambda: [])
    out = usage_report.render(usage_report.aggregate())
    assert "Ti Token 用量彙總" in out and "依 Provider" in out
