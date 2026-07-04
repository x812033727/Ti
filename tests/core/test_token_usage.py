"""Token 用量記錄與統計：events / providers(OpenAI) / experts(Claude) / history 聚合 / report。

並涵蓋任務 #2 的驗收守護：_counting_broadcast 依 task_id 聚合 per-task token/cost、
並行雙 lane 同 provider 同時發 token_usage 時不串戶、舊事件形狀（無 task_id）行為不變。
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from studio import events, experts, history, providers, usage_report
from studio.events import EventType
from studio.orchestrator import LaneContext, StudioSession
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


def test_token_usage_event_accepts_optional_task_id():
    ev = events.token_usage(
        "s",
        "engineer",
        "minimax",
        "MiniMax-M3",
        10,
        5,
        15,
        cost_usd=0.1,
        task_id=7,
    )
    assert ev.payload["task_id"] == 7

    old_shape = events.token_usage("s", "engineer", "minimax", "MiniMax-M3", 10, 5, 15)
    assert "task_id" not in old_shape.payload


@pytest.mark.asyncio
async def test_tagged_broadcast_injects_task_id_into_token_usage_payload():
    from studio.orchestrator import LaneContext, StudioSession

    bucket, sink = collect()
    session = StudioSession("s", sink, cwd=None)
    ctx = LaneContext("main", None, {})
    tag = session._lane_tag(ctx, {"id": 42, "title": "序列任務"})
    await session._tagged_broadcast(tag, token_usage_task_id=42)(
        events.token_usage("s", "engineer", "minimax", "MiniMax-M3", 10, 5, 15)
    )

    assert bucket[0].payload["task_id"] == 42


@pytest.mark.asyncio
async def test_tagged_broadcast_does_not_inject_main_lane_task_id_into_non_token_events():
    from studio.orchestrator import LaneContext, StudioSession

    bucket, sink = collect()
    session = StudioSession("s", sink, cwd=None)
    ctx = LaneContext("main", None, {})
    tag = session._lane_tag(ctx, {"id": 42, "title": "序列任務"})
    await session._tagged_broadcast(tag, token_usage_task_id=42)(
        events.expert_message("s", "engineer", "工程師", "E", "done")
    )

    assert "task_id" not in bucket[0].payload


@pytest.mark.asyncio
async def test_tagged_broadcast_keeps_parallel_lane_task_id_on_non_token_events():
    from studio.orchestrator import LaneContext, StudioSession

    bucket, sink = collect()
    session = StudioSession("s", sink, cwd=None)
    ctx = LaneContext("lane-s-42", None, {})
    tag = session._lane_tag(ctx, {"id": 42, "title": "並行任務"})
    await session._tagged_broadcast(tag, token_usage_task_id=42)(
        events.expert_message("s", "engineer", "工程師", "E", "done")
    )

    assert bucket[0].payload["task_id"] == 42


@pytest.mark.asyncio
async def test_speak_in_main_lane_injects_task_id_only_into_token_usage():
    from studio.orchestrator import LaneContext, StudioSession

    class FakeExpert:
        async def speak(self, _prompt, broadcast):
            await broadcast(events.expert_message("s", "engineer", "工程師", "E", "done"))
            await broadcast(events.token_usage("s", "engineer", "minimax", "MiniMax-M3", 1, 2, 3))
            return "ok"

    bucket, sink = collect()
    session = StudioSession("s", sink, experts={"engineer": FakeExpert()}, cwd=None)
    task = {"id": 42, "title": "序列任務"}
    ctx = LaneContext("main", None, session._get_experts())
    tag = session._lane_tag(ctx, task)

    await session._speak(ctx, "engineer", "prompt", tag, token_usage_task_id=task["id"])

    assert "task_id" not in bucket[0].payload
    assert bucket[1].payload["task_id"] == 42


@pytest.mark.asyncio
async def test_tagged_broadcast_preserves_existing_token_usage_task_id():
    from studio.orchestrator import StudioSession

    bucket, sink = collect()
    session = StudioSession("s", sink, cwd=None)
    await session._tagged_broadcast(42)(
        events.token_usage("s", "engineer", "minimax", "MiniMax-M3", 10, 5, 15, task_id=99)
    )

    assert bucket[0].payload["task_id"] == 99


# --- history._derive_token_usage 聚合 ------------------------------------


def test_derive_token_usage_aggregates_by_group():
    evs = [
        {"type": "expert_message", "payload": {"text": "hi"}},  # 非 token_usage 應忽略
        {
            "type": "token_usage",
            "payload": {
                "speaker": "engineer",
                "provider": "minimax",
                "model": "MiniMax-M3",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cost_usd": None,
            },
        },
        {
            "type": "token_usage",
            "payload": {
                "speaker": "pm",
                "provider": "claude",
                "model": "claude-opus-4-8",
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "total_tokens": 250,
                "cost_usd": 0.5,
                "cache_read": 800,
                "cache_write": 40,
            },
        },
        {
            "type": "token_usage",
            "payload": {
                "speaker": "engineer",
                "provider": "minimax",
                "model": "MiniMax-M3",
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "cost_usd": None,
            },
        },
    ]
    tu = history._derive_token_usage(evs)
    assert tu["total"] == {
        "prompt": 310,
        "completion": 74,
        "total": 384,
        "cost_usd": 0.5,
        "calls": 3,
        "cache_read": 800,
        "cache_write": 40,
    }
    assert tu["by_provider"]["minimax"]["total"] == 134
    assert tu["by_provider"]["minimax"]["calls"] == 2
    assert tu["by_provider"]["claude"]["cost_usd"] == 0.5
    # 快取量只歸 claude 桶（minimax 事件無 cache 欄位 → 0）
    assert tu["by_provider"]["claude"]["cache_read"] == 800
    assert tu["by_provider"]["claude"]["cache_write"] == 40
    assert tu["by_provider"]["minimax"]["cache_read"] == 0
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
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 50,
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
    chat = _SeqChat(
        [
            _resp(
                tool_calls=[_tc("1", "read_file", '{"path": "a.py"}')], usage=_usage(100, 0, 100)
            ),
            _resp(content="完成", usage=_usage(30, 20, 50)),
        ]
    )
    expert = providers.OpenAIExpert(
        BY_KEY["engineer"], "t", tmp_path, chat=chat, model="MiniMax-M3"
    )
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
    chat = _SeqChat(
        [
            # attempt 1：先累加 7，再撞限流 → 整個 _attempt 重放
            _resp(tool_calls=[_tc("1", "read_file", '{"path": "a"}')], usage=_usage(7, 0, 7)),
            rl,
            # attempt 2（重放）：直接成功，usage=10
            _resp(content="OK", usage=_usage(10, 0, 10)),
        ]
    )
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
        {
            "session_id": "a",
            "started_at": 1000,
            "token_usage": {
                "total": {
                    "prompt": 1_000_000,
                    "completion": 500_000,
                    "total": 1_500_000,
                    "cost_usd": 0.0,
                    "calls": 1,
                },
                "by_provider": {
                    "minimax": {
                        "prompt": 1_000_000,
                        "completion": 500_000,
                        "total": 1_500_000,
                        "cost_usd": 0.0,
                        "calls": 1,
                    }
                },
                "by_model": {
                    "MiniMax-M3": {
                        "prompt": 1_000_000,
                        "completion": 500_000,
                        "total": 1_500_000,
                        "cost_usd": 0.0,
                        "calls": 1,
                    }
                },
                "by_role": {
                    "engineer": {
                        "prompt": 1_000_000,
                        "completion": 500_000,
                        "total": 1_500_000,
                        "cost_usd": 0.0,
                        "calls": 1,
                    }
                },
            },
        },
        {
            "session_id": "b",
            "started_at": 2000,
            "token_usage": {
                "total": {
                    "prompt": 100,
                    "completion": 50,
                    "total": 150,
                    "cost_usd": 0.9,
                    "calls": 1,
                },
                "by_provider": {
                    "claude": {
                        "prompt": 100,
                        "completion": 50,
                        "total": 150,
                        "cost_usd": 0.9,
                        "calls": 1,
                    }
                },
                "by_model": {
                    "claude-opus-4-8": {
                        "prompt": 100,
                        "completion": 50,
                        "total": 150,
                        "cost_usd": 0.9,
                        "calls": 1,
                    }
                },
                "by_role": {
                    "pm": {
                        "prompt": 100,
                        "completion": 50,
                        "total": 150,
                        "cost_usd": 0.9,
                        "calls": 1,
                    }
                },
            },
        },
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


def test_cache_hit_pct_math():
    # 命中率＝cache_read /（prompt ＋ cache_read ＋ cache_write）
    assert usage_report._cache_hit_pct(
        {"prompt": 100, "cache_read": 300, "cache_write": 100}
    ) == pytest.approx(60.0)
    # 分母為 0（無任何 input）→ 0，不得除零
    assert usage_report._cache_hit_pct({"prompt": 0, "cache_read": 0, "cache_write": 0}) == 0.0
    # 舊桶缺欄位 → 視為 0
    assert usage_report._cache_hit_pct({"prompt": 0}) == 0.0


def test_usage_report_aggregates_and_renders_cache(monkeypatch):
    metas = [
        {
            "session_id": "c",
            "started_at": 3000,
            "token_usage": {
                "total": {
                    "prompt": 200,
                    "completion": 50,
                    "total": 250,
                    "cost_usd": 0.5,
                    "calls": 1,
                    "cache_read": 800,
                    "cache_write": 40,
                },
                "by_provider": {
                    "claude": {
                        "prompt": 200,
                        "completion": 50,
                        "total": 250,
                        "cost_usd": 0.5,
                        "calls": 1,
                        "cache_read": 800,
                        "cache_write": 40,
                    }
                },
                "by_model": {},
                "by_role": {},
            },
        },
        # 舊 session：聚合桶完全沒有 cache_read/cache_write，須以 0 計、不報錯
        {
            "session_id": "old",
            "started_at": 3100,
            "token_usage": {
                "total": {
                    "prompt": 100,
                    "completion": 10,
                    "total": 110,
                    "cost_usd": 0.1,
                    "calls": 1,
                },
                "by_provider": {
                    "claude": {
                        "prompt": 100,
                        "completion": 10,
                        "total": 110,
                        "cost_usd": 0.1,
                        "calls": 1,
                    }
                },
                "by_model": {},
                "by_role": {},
            },
        },
    ]
    monkeypatch.setattr(usage_report.history, "list_sessions", lambda: metas)
    agg = usage_report.aggregate()
    assert agg["total"]["cache_read"] == 800 and agg["total"]["cache_write"] == 40
    # 命中率＝800 /（300 prompt ＋ 800 ＋ 40）= 800/1140
    assert usage_report._cache_hit_pct(agg["total"]) == pytest.approx(800 / 1140 * 100)
    out = usage_report.render(agg)
    assert "Prompt 快取" in out and "cache_hit=" in out


# --- usage_report：events fallback 與 CLI entrypoint --------------------


def test_usage_report_derives_from_events_when_meta_missing(monkeypatch):
    """meta 無 token_usage 但有 session_id → 回讀 events 即時重算並納入彙總（_usage_for fallback）。"""
    derived = {
        "total": {"prompt": 10, "completion": 5, "total": 15, "cost_usd": 0.0, "calls": 1},
        "by_provider": {},
        "by_model": {},
        "by_role": {},
    }
    monkeypatch.setattr(
        usage_report.history, "list_sessions", lambda: [{"session_id": "z", "started_at": 0}]
    )
    monkeypatch.setattr(usage_report.history, "load_events", lambda sid: ["ev"])
    monkeypatch.setattr(usage_report.history, "_derive_token_usage", lambda evs: derived)
    agg = usage_report.aggregate()
    assert agg["sessions"] == 1 and agg["total"]["total"] == 15


def test_usage_report_skips_session_without_usage(monkeypatch):
    """meta 無 token_usage 且 derive 也無 calls → 該場跳過、不計入。"""
    empty = {"total": {"calls": 0}, "by_provider": {}, "by_model": {}, "by_role": {}}
    monkeypatch.setattr(
        usage_report.history, "list_sessions", lambda: [{"session_id": "z", "started_at": 0}]
    )
    monkeypatch.setattr(usage_report.history, "load_events", lambda sid: [])
    monkeypatch.setattr(usage_report.history, "_derive_token_usage", lambda evs: empty)
    assert usage_report.aggregate()["sessions"] == 0


def test_usage_report_skips_meta_without_session_id(monkeypatch):
    """meta 既無 token_usage 又無 session_id → 無從重算，回 None 跳過。"""
    monkeypatch.setattr(usage_report.history, "list_sessions", lambda: [{"started_at": 0}])
    assert usage_report.aggregate()["sessions"] == 0


def test_usage_report_main_renders_text(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(usage_report.config, "HISTORY_ROOT", tmp_path)  # 存在 → 不早退
    monkeypatch.setattr(usage_report.history, "list_sessions", lambda: [])
    assert usage_report.main([]) == 0
    assert "Ti Token 用量彙總" in capsys.readouterr().out


def test_usage_report_main_json_and_since(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(usage_report.config, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(usage_report.history, "list_sessions", lambda: [])
    assert usage_report.main(["--json", "--since", "2026-01-01"]) == 0
    assert '"sessions"' in capsys.readouterr().out  # JSON 輸出


def test_usage_report_main_missing_history_returns_1(monkeypatch, tmp_path):
    monkeypatch.setattr(usage_report.config, "HISTORY_ROOT", tmp_path / "nope")  # 不存在
    assert usage_report.main([]) == 1


# --- _counting_broadcast 依 task_id 聚合 per-task token/cost（任務 #2 守護）----


@pytest.mark.asyncio
async def test_counting_broadcast_aggregates_single_task_id():
    """同一 task 多輪 token_usage 累加 input/output/total, cost_source 留為 reported。

    對應驗收標準 1（payload 帶 task_id 即聚合）+ 隱性合約 3（欄位命名 input/output/total_tokens）。
    """
    sink_calls: list[events.StudioEvent] = []

    async def sink(ev):
        sink_calls.append(ev)

    s = StudioSession("s-aggr", sink, cwd=None)
    s._main_ctx = LaneContext("main", None, {})

    # 主 lane task: _tagged_broadcast 透過 token_usage_task_id 補 task_id
    bc = s._tagged_broadcast(s._lane_tag(s._main_ctx, {"id": 7}), token_usage_task_id=7)
    await bc(
        events.token_usage("s-aggr", "engineer", "minimax", "MiniMax-M3", 100, 20, 120, cost_usd=0.1)
    )
    await bc(
        events.token_usage("s-aggr", "engineer", "minimax", "MiniMax-M3", 50, 30, 80, cost_usd=0.2)
    )

    perf = s._task_perf[7]
    assert perf["input_tokens"] == 150
    assert perf["output_tokens"] == 50
    assert perf["total_tokens"] == 200
    assert perf["cost_usd"] == pytest.approx(0.30)
    assert perf["cost_source"] == "reported"
    # session 層合計一致
    assert s._tokens_used == 200
    assert s._usd_used == pytest.approx(0.30)
    # sink 收到兩事件, payload 都帶 task_id=7
    assert [e.payload["task_id"] for e in sink_calls if e.type == EventType.TOKEN_USAGE] == [7, 7]


@pytest.mark.asyncio
async def test_counting_broadcast_event_without_task_id_does_not_pollute_perf():
    """舊事件形狀（payload 無 task_id）→ 不寫入任何 _task_perf entry,但 session 累計照算。

    守護驗收標準 5 的對偶：舊格式事件不污染新欄位的聚合路徑,行為與現行 _derive_token_usage
    一致（session-level 累計在 _tokens_used/_usd_used,非 per-task）。
    """
    s = StudioSession("s-legacy", _noop_sink, cwd=None)
    s._main_ctx = LaneContext("main", None, {})

    # 直接送 token_usage(走 self.broadcast = self._counting_broadcast),不透過 _tagged_broadcast
    await s.broadcast(
        events.token_usage("s-legacy", "engineer", "minimax", "MiniMax-M3", 100, 50, 150, cost_usd=0.0)
    )

    assert s._task_perf == {}  # 無 task_id → 不寫入任何 per-task entry
    assert s._tokens_used == 150
    assert s._usd_used == 0.0


@pytest.mark.asyncio
async def test_counting_broadcast_event_without_cost_keeps_perf_cost_none():
    """cost_usd 缺/None → per-task 欄位 cost_usd=None、cost_source=None,session USD 不動。"""
    sink_calls: list[events.StudioEvent] = []

    async def sink(ev):
        sink_calls.append(ev)

    s = StudioSession("s-nocost", sink, cwd=None)
    s._main_ctx = LaneContext("main", None, {})
    bc = s._tagged_broadcast(s._lane_tag(s._main_ctx, {"id": 11}), token_usage_task_id=11)

    # cost_usd 預設為 None(不傳)
    await bc(
        events.token_usage("s-nocost", "engineer", "openai", "gpt-5", 50, 10, 60)
    )
    perf = s._task_perf[11]
    assert perf["input_tokens"] == 50 and perf["output_tokens"] == 10 and perf["total_tokens"] == 60
    assert perf["cost_usd"] is None  # 缺成本資料=考核旁路永不 raise
    assert perf["cost_source"] is None
    assert s._usd_used == 0.0


@pytest.mark.asyncio
async def test_counting_broadcast_malformed_payload_does_not_break_event_flow():
    """payload 欄位異常（cost_usd 是字串）→ _counting_broadcast 吞掉例外,sink 照收。

    容錯：_counting_broadcast 絕不讓計數阻斷事件流（既有合約）。
    """
    sink_calls: list[events.StudioEvent] = []

    async def sink(ev):
        sink_calls.append(ev)

    s = StudioSession("s-malformed", sink, cwd=None)
    s._main_ctx = LaneContext("main", None, {})
    bc = s._tagged_broadcast(s._lane_tag(s._main_ctx, {"id": 99}), token_usage_task_id=99)

    # 手搓畸形 payload:cost_usd 是字串、total_tokens 是 None
    bad = events.StudioEvent(
        EventType.TOKEN_USAGE,
        "s-malformed",
        {
            "speaker": "engineer",
            "provider": "minimax",
            "model": "MiniMax-M3",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": None,  # 觸發 fallback = input+output
            "cost_usd": "not-a-number",  # 觸發 TypeError/ValueError
            "task_id": 99,
        },
    )
    await bc(bad)

    # sink 照收到、task_perf 落地(總量 = 10+5=15 fallback)
    assert any(e.type == EventType.TOKEN_USAGE for e in sink_calls)
    perf = s._task_perf[99]
    assert perf["input_tokens"] == 10
    assert perf["output_tokens"] == 5
    assert perf["total_tokens"] == 15  # fallback: prompt + completion
    # cost 異常 → cost_usd 維持 None、cost_source 維持 None
    assert perf["cost_usd"] is None
    assert perf["cost_source"] is None


@pytest.mark.asyncio
async def test_parallel_lanes_same_provider_token_attribution_no_cross_contamination():
    """驗收標準 2：並行兩 lane(lane_id≠main)、同 provider 同時發 token_usage,
    _task_perf[task_id] 各自歸因正確、絕不串戶。

    黑白對照樣本：兩條 lane 各送一筆 token_usage(provider/minimax 相同,
    task_id 不同),透過 _tagged_broadcast 標 task_id,並以 asyncio.gather 模擬
    scheduler 同時派發;後續斷言 A 的數字只到 _task_perf[A]、B 只到 _task_perf[B]。
    """
    sink_calls: list[events.StudioEvent] = []

    async def sink(ev):
        sink_calls.append(ev)

    s = StudioSession("s-parallel", sink, cwd=None)
    s._main_ctx = LaneContext("main", None, {})
    # 兩條並行 lane(lane_id 非 "main",以 _lane_tag 走 task_id 分流)
    lane_a = LaneContext("lane-s-a", None, {}, branch="lane-s-a")
    lane_b = LaneContext("lane-s-b", None, {}, branch="lane-s-b")
    s._lane_ctxs = [s._main_ctx, lane_a, lane_b]

    task_a = {"id": 100, "title": "並行任務A"}
    task_b = {"id": 200, "title": "並行任務B"}

    bc_a = s._tagged_broadcast(
        s._lane_tag(lane_a, task_a), token_usage_task_id=task_a["id"]
    )
    bc_b = s._tagged_broadcast(
        s._lane_tag(lane_b, task_b), token_usage_task_id=task_b["id"]
    )

    # 同 provider(都是 minimax)、同時發：刻意做"兩 lane 數字差不一樣大"以便任何串戶都現形
    ev_a = events.token_usage(
        "s-parallel", "engineer", "minimax", "MiniMax-M3", 1000, 500, 1500, cost_usd=0.30
    )
    ev_b = events.token_usage(
        "s-parallel", "engineer", "minimax", "MiniMax-M3", 2000, 800, 2800, cost_usd=0.60
    )

    # 真正並發：scheduler 同時派發兩 lane 的 broadcast
    await asyncio.gather(bc_a(ev_a), bc_b(ev_b))

    # 黑白對照：A 的數字絕不跑到 B、B 的也不會跑到 A
    perf_a = s._task_perf[100]
    perf_b = s._task_perf[200]

    assert perf_a["input_tokens"] == 1000
    assert perf_a["output_tokens"] == 500
    assert perf_a["total_tokens"] == 1500
    assert perf_a["cost_usd"] == pytest.approx(0.30)
    assert perf_a["cost_source"] == "reported"

    assert perf_b["input_tokens"] == 2000
    assert perf_b["output_tokens"] == 800
    assert perf_b["total_tokens"] == 2800
    assert perf_b["cost_usd"] == pytest.approx(0.60)
    assert perf_b["cost_source"] == "reported"

    # session-wide 累計:兩者總和,不分屬
    assert s._tokens_used == 1500 + 2800
    assert s._usd_used == pytest.approx(0.90)

    # sink 收到的兩個事件 payload 上的 task_id 各自正確(未互相污染)
    payloads = [
        e.payload for e in sink_calls if e.type == events.EventType.TOKEN_USAGE
    ]
    assert {p["task_id"] for p in payloads} == {100, 200}
    assert all(p["provider"] == "minimax" for p in payloads)  # 同 provider 同時發也未混


@pytest.mark.asyncio
async def test_parallel_lanes_interleaved_aggregation_still_correct():
    """三條並行 lane 交錯送多筆 token_usage,各 task_id 各自的 input/output 仍正確加總。

    補黑白樣本:不只"一筆 vs 一筆",而是每條 lane 送多筆不同大小的事件,
    模擬實際 session 中多次 LLM 呼叫的歸因。
    """
    s = StudioSession("s-interleave", _noop_sink, cwd=None)
    s._main_ctx = LaneContext("main", None, {})
    lane_a = LaneContext("lane-i-a", None, {}, branch="lane-i-a")
    lane_b = LaneContext("lane-i-b", None, {}, branch="lane-i-b")
    lane_c = LaneContext("lane-i-c", None, {}, branch="lane-i-c")
    s._lane_ctxs = [s._main_ctx, lane_a, lane_b, lane_c]

    tasks = {1: lane_a, 2: lane_b, 3: lane_c}
    bcs = {
        tid: s._tagged_broadcast(s._lane_tag(ctx, {"id": tid}), token_usage_task_id=tid)
        for tid, ctx in tasks.items()
    }

    # 每條 lane 送三筆,交錯進行
    plan = [
        (1, 10, 1, 11, 0.01),
        (2, 20, 2, 22, 0.02),
        (3, 30, 3, 33, 0.03),
        (1, 11, 2, 13, 0.04),
        (2, 21, 3, 24, 0.05),
        (3, 31, 4, 35, 0.06),
        (1, 12, 3, 15, 0.07),
        (2, 22, 4, 26, 0.08),
        (3, 32, 5, 37, 0.09),
    ]
    coros = []
    for tid, inp, out, tot, cost in plan:
        coros.append(
            bcs[tid](
                events.token_usage(
                    "s-interleave", "engineer", "minimax", "MiniMax-M3",
                    inp, out, tot, cost_usd=cost,
                )
            )
        )
    await asyncio.gather(*coros)

    # 各 task_id 三輪加總
    assert s._task_perf[1]["input_tokens"] == 10 + 11 + 12
    assert s._task_perf[1]["output_tokens"] == 1 + 2 + 3
    assert s._task_perf[1]["total_tokens"] == 11 + 13 + 15
    assert s._task_perf[1]["cost_usd"] == pytest.approx(0.01 + 0.04 + 0.07)
    assert s._task_perf[1]["cost_source"] == "reported"

    assert s._task_perf[2]["input_tokens"] == 20 + 21 + 22
    assert s._task_perf[2]["output_tokens"] == 2 + 3 + 4
    assert s._task_perf[2]["total_tokens"] == 22 + 24 + 26
    assert s._task_perf[2]["cost_usd"] == pytest.approx(0.02 + 0.05 + 0.08)

    assert s._task_perf[3]["input_tokens"] == 30 + 31 + 32
    assert s._task_perf[3]["output_tokens"] == 3 + 4 + 5
    assert s._task_perf[3]["total_tokens"] == 33 + 35 + 37
    assert s._task_perf[3]["cost_usd"] == pytest.approx(0.03 + 0.06 + 0.09)


def test_collect_task_perf_initializes_token_fields_as_none_even_for_dispatch_bound_task():
    """_collect_task_perf 在 per-task 派工換綁情境(task_perf 已含 provider/model)下,
    仍確保 token/cost 欄位以 None 初始化(無 token_usage 事件時)。

    呼應驗收標準 3:缺 token 資料時欄位為 None,而非拋錯。
    """
    s = StudioSession("s-init", _noop_sink, cwd=None)
    s._main_ctx = LaneContext("main", None, {"engineer": SimpleNamespace(role=BY_KEY["engineer"])})
    # 模擬 _dispatch_task_expert 已先寫入 provider/model(provider/model 已有值)
    s._task_perf[1] = {"provider": "claude", "model": "claude-opus-4-8"}
    # 再走 _collect_task_perf(此情境為 huddle 重試或後續接續)
    task = {"id": 1, "title": "重試任務"}
    s._collect_task_perf(s._main_ctx, task, "engineer", 0.5)

    perf = s._task_perf[1]
    # 既有的 provider/model 不被覆蓋
    assert perf["provider"] == "claude"
    assert perf["model"] == "claude-opus-4-8"
    # token/cost 欄位補上為 None（缺資料＝None 不拋錯）
    assert perf["input_tokens"] is None
    assert perf["output_tokens"] is None
    assert perf["total_tokens"] is None
    assert perf["cost_usd"] is None
    assert perf["cost_source"] is None
    # duration 累加
    assert perf["duration_s"] == 0.5


async def _noop_sink(ev):
    return None


# --- 任務 #4 — 檔案級舊 jsonl 回放守護（ADR 2172 雙向）------------------


def test_legacy_jsonl_history_replay_usage_report_golden(monkeypatch, tmp_path):
    """任務 #4 — 檔案級舊 jsonl 回放守護（ADR 2172 雙向）。

    場景：早期版本（引入 task_id 歸因前）的歷史 session jsonl 仍躺在 HISTORY_ROOT，
    需保證新版 pipeline（history.load_events → _derive_token_usage → usage_report.aggregate）
    仍能完整重算並彙總,行為與「補上 task_id」後的新版事件 bit-for-bit 一致。

    向一（舊→新相容）：舊格式 jsonl（payload 無 task_id、無 cache_* 等新欄位）→
        真實 history 載入 → _derive_token_usage → usage_report.aggregate() 全鏈路，
        輸出等於固定字面值黃金 dict。
    向二（新→舊相容）：同份 jsonl 每筆 token_usage payload 補上 task_id 後重跑同一條鏈，
        輸出仍等於同一個黃金 dict（task_id 是 per-task 維度,不污染 provider/model/role 彙總）。

    黃金 dict 採字面值（非快照檔），契約可讀、diff 可審。

    範圍守門（任務 #4 其餘驗收已由既有測試覆蓋,本測試只守「檔案級舊 jsonl 端到端回放」）：
      - 並行雙 lane 同 provider 不串戶 → test_parallel_lanes_same_provider_token_attribution_no_cross_contamination
      - 三 lane 交錯加總       → test_parallel_lanes_interleaved_aggregation_still_correct
      - 舊事件無 task_id 不污染 _task_perf → test_counting_broadcast_event_without_task_id_does_not_pollute_perf
      - usage_report meta 缺 token_usage 時回讀 events fallback
        → test_usage_report_derives_from_events_when_meta_missing
      - meta 完全無 session_id 跳過       → test_usage_report_skips_meta_without_session_id

    實作重點：
      - HISTORY_ROOT → tmp_path（兩處引用同一個 config 屬性,設一次即可,雙設為保險）。
      - 不 monkeypatch history.list_sessions / load_events / _derive_token_usage,走真實檔案 IO。
      - meta.json 不含 token_usage 區塊 → 強迫 _usage_for 走 fallback 分支,才能測到
        「load_events → _derive_token_usage」這條讀取鏈（含了會走 meta 短路,測不到）。
      - cost_usd 用整數美分值（5、1 → 6.0）,避免浮點累加不穩;model 採 claude 系列
        （無 MiniMax 列價,est_extra_usd=0.0,黃金基準乾淨）。
    """
    import json

    from studio import config

    sid = "legacy-replay-001"
    started_at = 1_700_000_000.0  # 固定時間,避免 aggregate(since=...) 漂移

    # 舊格式 token_usage payload：故意不帶 task_id、不帶 cache_* 等新欄位,
    # 模擬「引入 task_id 歸因前」的真實歷史場次。
    legacy_events = [
        {
            "type": "token_usage",
            "session_id": sid,
            "payload": {
                "speaker": "engineer",
                "provider": "claude",
                "model": "claude-sonnet-4-7",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost_usd": 5,  # 整數美分值,避免浮點累加誤差
            },
        },
        {
            "type": "token_usage",
            "session_id": sid,
            "payload": {
                "speaker": "pm",
                "provider": "claude",
                "model": "claude-sonnet-4-7",
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "cost_usd": 1,
            },
        },
    ]

    jsonl = tmp_path / f"{sid}.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps(ev, ensure_ascii=False) for ev in legacy_events) + "\n",
        encoding="utf-8",
    )

    # meta.json 不含 token_usage 區塊：強迫 _usage_for 走「回讀 events 即時重算」分支,
    # 才能測到 history.load_events → _derive_token_usage → aggregate 整條 fallback 鏈。
    meta = {
        "session_id": sid,
        "requirement": "任務#4 舊格式 jsonl 回放守護",
        "started_at": started_at,
        "status": "completed",
        "n_events": len(legacy_events),
    }
    (tmp_path / f"{sid}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )

    # 真實 HISTORY_ROOT 指向 tmp_path；list_sessions/load_events/_derive_token_usage
    # 都不 monkeypatch,走真正的檔案 IO 全鏈路。
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path)
    # 雙設保險：usage_report 與 history 內部都 from . import config,實為同一屬性,
    # 但顯式兩處設可避免將來 import 結構變更後單點失效。
    monkeypatch.setattr(usage_report.config, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(history.config, "HISTORY_ROOT", tmp_path)

    golden = {
        "sessions": 1,
        "total": {
            "prompt": 120,
            "completion": 60,
            "total": 180,
            "cost_usd": 6.0,
            "calls": 2,
            "cache_read": 0,
            "cache_write": 0,
        },
        "by_provider": {
            "claude": {
                "prompt": 120, "completion": 60, "total": 180,
                "cost_usd": 6.0, "calls": 2,
                "cache_read": 0, "cache_write": 0,
            },
        },
        "by_model": {
            "claude-sonnet-4-7": {
                "prompt": 120, "completion": 60, "total": 180,
                "cost_usd": 6.0, "calls": 2,
                "cache_read": 0, "cache_write": 0,
            },
        },
        "by_role": {
            "engineer": {
                "prompt": 100, "completion": 50, "total": 150,
                "cost_usd": 5.0, "calls": 1,
                "cache_read": 0, "cache_write": 0,
            },
            "pm": {
                "prompt": 20, "completion": 10, "total": 30,
                "cost_usd": 1.0, "calls": 1,
                "cache_read": 0, "cache_write": 0,
            },
        },
        "est_extra_usd": 0.0,
    }

    # ── 向一：舊事件（無 task_id）────────────────────────────────────
    agg_legacy = usage_report.aggregate()
    assert agg_legacy == golden, f"舊格式 jsonl 回放輸出與黃金基準不一致:\n{agg_legacy}"

    # ── 向二：補上 task_id 後重跑同一條鏈,輸出必須仍等於同一個黃金 dict
    #          （task_id 是 per-task 維度,不應影響 provider/model/role 彙總）。
    augmented_events = []
    for ev in legacy_events:
        new_ev = json.loads(json.dumps(ev))  # 深拷貝
        new_ev["payload"]["task_id"] = 7     # 補 task_id,模擬新版事件形狀
        augmented_events.append(new_ev)
    jsonl.write_text(
        "\n".join(json.dumps(ev, ensure_ascii=False) for ev in augmented_events) + "\n",
        encoding="utf-8",
    )
    meta["n_events"] = len(augmented_events)
    (tmp_path / f"{sid}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )

    agg_with_task_id = usage_report.aggregate()
    assert agg_with_task_id == golden, (
        f"補 task_id 後輸出漂移（task_id 不應污染 provider/model/role 彙總）:\n"
        f"{agg_with_task_id}"
    )
    # 額外顯式斷言兩輪結果彼此相等（防黃金基準寫錯時雙方一起錯過）
    assert agg_legacy == agg_with_task_id
