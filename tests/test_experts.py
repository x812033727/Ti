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
    # 解除 PM 模型釘選（預設釘 claude-fable-5，另測 tests/core/test_pm_pin.py），驗證 LEAD 二分法。
    monkeypatch.setattr(config, "PM_PIN_MODEL", "")
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


# --- _build_client 接線回歸守門（lane 隔離真正防線必須被接上）-----------
# 背景：lane 隔離靠 PreToolUse fs-guard hook（can_use_tool 對預先允許/acceptEdits 的寫檔
# 工具不觸發）。這條接線一旦被改掉，lane 成果會無聲漏進主工作樹。此測試以假 SDK 捕捉
# _build_client 實際傳入的 ClaudeAgentOptions，確認 hook 有接上且綁定到傳入的 cwd。
def _install_fake_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class ClaudeSDKClient:
        def __init__(self, options=None):
            self.options = options

    class HookMatcher:
        def __init__(self, matcher=None, hooks=None):
            self.matcher = matcher
            self.hooks = hooks or []

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ClaudeSDKClient = ClaudeSDKClient
    mod.HookMatcher = HookMatcher
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


def _deny(out) -> bool:
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


async def test_build_client_wires_pretooluse_fs_guard_bound_to_cwd(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch)
    lane = tmp_path / "proj.lanes" / "task-1"
    lane.mkdir(parents=True)

    client = experts._build_client(BY_KEY["engineer"], "sid", lane)
    hooks = client.options.hooks
    assert "PreToolUse" in hooks, "PreToolUse hook 未接上——lane 隔離防線失效"
    guard = hooks["PreToolUse"][0].hooks[0]

    # 綁定到傳入的 cwd：寫到兄弟目錄（主工作樹）→ deny；寫到 cwd 內 → 放行
    sibling = str(tmp_path / "proj" / "leak.py")
    assert _deny(
        await guard({"tool_name": "Write", "tool_input": {"file_path": sibling}}, "id", None)
    )
    assert not _deny(
        await guard({"tool_name": "Write", "tool_input": {"file_path": "ok.py"}}, "id", None)
    )


# --- effective_model：任務結果的模型可見性 --------------------------------------


def test_expert_effective_model(monkeypatch):
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd, model="": object())
    monkeypatch.setattr(config, "MODEL_FAST", "claude-sonnet-4-6")
    monkeypatch.setattr(config, "ROLE_MODELS", {})
    # 無覆寫 → 角色模型槽（engineer 非 LEAD → FAST）；有覆寫 → 覆寫優先。
    assert (
        experts.Expert(BY_KEY["engineer"], "s", "/tmp/x").effective_model() == "claude-sonnet-4-6"
    )
    assert (
        experts.Expert(
            BY_KEY["engineer"], "s", "/tmp/x", model="claude-haiku-4-5"
        ).effective_model()
        == "claude-haiku-4-5"
    )
    # PM 釘選最優先。
    monkeypatch.setattr(config, "PM_PIN_MODEL", "claude-fable-5")
    assert experts.Expert(BY_KEY["pm"], "s", "/tmp/x").effective_model() == "claude-fable-5"


# --- _emit_claude_token_usage：duration_api_ms 接點覆蓋（任務 #2）----------
# 守門意圖：若 SDK 改欄位名（例如 duration_api_ms → api_duration_ms），getattr 會靜默回
# None，整條 latency 功能無聲失效。以下兩測驗在「正路徑帶值」與「缺屬性→不落地」兩端各釘
# 一根，確保任何此類迴歸都能立即被 CI 抓住，不需靠運氣。


async def test_emit_claude_token_usage_with_duration_api_ms():
    """msg 帶 duration_api_ms=1234 → payload["duration_ms"] == 1234（正路徑自證對應）。"""
    usage = types.SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    msg = types.SimpleNamespace(
        usage=usage,
        duration_api_ms=1234,
        total_cost_usd=0.01,
    )
    bucket, broadcast = collect()
    await experts._emit_claude_token_usage(msg, "sid", BY_KEY["engineer"], broadcast)

    assert len(bucket) == 1, f"應發出 1 個事件，got {len(bucket)}"
    payload = bucket[0].to_dict()["payload"]
    assert payload["duration_ms"] == 1234, (
        f"duration_api_ms=1234 應映射為 duration_ms=1234，got {payload.get('duration_ms')!r}"
    )
    # 附帶確認 provider/model/speaker 等基本欄位在場
    assert payload["provider"] == "claude"
    assert payload["speaker"] == "engineer"
    assert payload["prompt_tokens"] == 100
    assert payload["completion_tokens"] == 20


async def test_emit_claude_token_usage_without_duration_api_ms():
    """msg 無 duration_api_ms 屬性 → payload 不含 duration_ms 鍵（None 不落地）。"""
    usage = types.SimpleNamespace(
        input_tokens=50,
        output_tokens=10,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    # 故意不設 duration_api_ms 屬性，模擬舊版 SDK 或回傳不含時延的 ResultMessage
    msg = types.SimpleNamespace(
        usage=usage,
        total_cost_usd=0.005,
    )
    bucket, broadcast = collect()
    await experts._emit_claude_token_usage(msg, "sid", BY_KEY["engineer"], broadcast)

    assert len(bucket) == 1, f"應發出 1 個事件，got {len(bucket)}"
    payload = bucket[0].to_dict()["payload"]
    assert "duration_ms" not in payload, (
        f"無 duration_api_ms 時 payload 不應含 duration_ms 鍵，got payload={payload!r}"
    )
