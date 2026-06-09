"""experts.py 單元測試。

不需 claude-agent-sdk：純函式（_summarize_tool / _model_for）直接測；需要 SDK 類別的
路徑（stream_to_events 的 isinstance 判型、Expert 生命週期）以注入縫驗證——
stream_to_events 在 sys.modules 注入假 claude_agent_sdk 模組，Expert 則 monkeypatch
experts._build_client。沿用 test_orchestrator.py 的 collect() 慣例。
"""

from __future__ import annotations

import sys
import types

import pytest

from studio import config, events, experts
from studio.events import EventType
from studio.roles import BY_KEY

# --- 共用 ---------------------------------------------------------------


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


async def _agen(items):
    for it in items:
        yield it


# --- _summarize_tool（純函式，無 SDK）-----------------------------------


@pytest.mark.parametrize(
    "name, tool_input, expected",
    [
        ("Write", {"file_path": "/a/b/foo.py"}, "寫入 foo.py"),
        ("Edit", {"file_path": "/a/b/foo.py"}, "修改 foo.py"),
        ("Read", {"file_path": "/a/b/foo.py"}, "讀取 foo.py"),
        ("Read", {"path": "/x/bar.txt"}, "讀取 bar.txt"),
        ("Bash", {"command": "ls -la"}, "執行: ls -la"),
        ("Bash", {"command": "echo hi\nrm -rf /"}, "執行: echo hi"),
        ("Grep", {"pattern": "TODO"}, "Grep: TODO"),
        ("Glob", {"pattern": "**/*.py"}, "Glob: **/*.py"),
        ("WebFetch", {}, "WebFetch"),
        ("Write", {}, "Write"),  # 無 file_path/path → 退回 name
    ],
)
def test_summarize_tool(name, tool_input, expected):
    assert experts._summarize_tool(name, tool_input) == expected


def test_summarize_tool_bash_truncates_long_command():
    long_cmd = "x" * 300
    out = experts._summarize_tool("Bash", {"command": long_cmd})
    assert out == "執行: " + "x" * 120


# --- _model_for ---------------------------------------------------------


def test_model_for_lead_vs_fast(monkeypatch):
    monkeypatch.setattr(config, "LEAD_ROLES", {"pm"})
    monkeypatch.setattr(config, "MODEL_LEAD", "lead-model")
    monkeypatch.setattr(config, "MODEL_FAST", "fast-model")
    assert experts._model_for(BY_KEY["pm"]) == "lead-model"
    assert experts._model_for(BY_KEY["engineer"]) == "fast-model"


# --- stream_to_events：注入假 SDK 類別讓 isinstance 成立 -----------------


@pytest.fixture
def fake_sdk(monkeypatch):
    """在 sys.modules 注入假的 claude_agent_sdk，含與真 SDK 同名的訊息/區塊類別。

    stream_to_events 內 `from claude_agent_sdk import ...` 會取到這些假類別，
    使 isinstance 判型在測試裡成立。回傳建構小幫手供測試組裝訊息。
    """
    mod = types.ModuleType("claude_agent_sdk")

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        pass

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


async def test_stream_to_events_emits_message_and_tool_events(fake_sdk):
    role = BY_KEY["engineer"]
    msgs = [
        fake_sdk.AssistantMessage(
            content=[
                fake_sdk.TextBlock("  你好  "),
                fake_sdk.ToolUseBlock("Write", {"file_path": "/w/main.py"}),
                fake_sdk.TextBlock(""),  # 空白文字應被略過
                fake_sdk.TextBlock("完成"),
            ]
        ),
        fake_sdk.ResultMessage(),
    ]
    bucket, broadcast = collect()

    text = await experts.stream_to_events(_agen(msgs), "sess1", role, broadcast)

    # 回傳值＝各非空 TextBlock 以 \n 串接（已 strip）
    assert text == "你好\n完成"

    # 事件序列：message(你好) → status:working + tool_use → message(完成)
    types_seq = [ev.type for ev in bucket]
    assert types_seq == [
        EventType.EXPERT_MESSAGE,
        EventType.EXPERT_STATUS,
        EventType.TOOL_USE,
        EventType.EXPERT_MESSAGE,
    ]
    assert bucket[0].payload["text"] == "你好"
    assert bucket[0].payload["speaker"] == role.key
    assert bucket[1].payload["status"] == "working"
    assert bucket[2].payload["tool"] == "Write"
    assert bucket[2].payload["summary"] == "寫入 main.py"
    assert bucket[3].payload["text"] == "完成"


async def test_stream_to_events_stops_at_result_message(fake_sdk):
    """遇 ResultMessage 即停，其後訊息一律忽略。"""
    role = BY_KEY["engineer"]
    msgs = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("前")]),
        fake_sdk.ResultMessage(),
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("後（應被忽略）")]),
    ]
    bucket, broadcast = collect()

    text = await experts.stream_to_events(_agen(msgs), "s", role, broadcast)

    assert text == "前"
    assert all("後" not in ev.payload.get("text", "") for ev in bucket)


# --- Expert 生命週期：monkeypatch _build_client，不需 SDK -----------------


class _FakeClient:
    def __init__(self):
        self.connects = 0
        self.disconnects = 0

    async def connect(self):
        self.connects += 1

    async def disconnect(self):
        self.disconnects += 1


@pytest.fixture
def fake_expert(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: client)
    exp = experts.Expert(BY_KEY["engineer"], "sess", "/tmp/x")
    return exp, client


async def test_start_is_idempotent(fake_expert):
    exp, client = fake_expert
    await exp.start()
    await exp.start()
    assert client.connects == 1


async def test_stop_before_start_is_noop(fake_expert):
    exp, client = fake_expert
    await exp.stop()
    assert client.disconnects == 0


async def test_stop_after_start_disconnects(fake_expert):
    exp, client = fake_expert
    await exp.start()
    await exp.stop()
    assert client.disconnects == 1
    # stop 後可再次 stop 而不重複斷線
    await exp.stop()
    assert client.disconnects == 1
