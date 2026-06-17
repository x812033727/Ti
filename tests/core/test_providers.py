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

    async def __call__(self, messages, tools, model, **_kw):
        # **_kw 容忍混用路徑經 _chat_for 帶入的 provider= 關鍵字（測試僅關心 messages/tools/model）。
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


def test_openai_model_for_minimax(monkeypatch):
    """PROVIDER=minimax 時走 MiniMax 模型槽（依 LEAD_ROLES 二分），不污染 openai 行為。"""
    monkeypatch.setattr(config, "PROVIDER", "minimax")
    monkeypatch.setattr(config, "MINIMAX_MODEL_LEAD", "MiniMax-M3")
    monkeypatch.setattr(config, "MINIMAX_MODEL_FAST", "MiniMax-M2.7")
    assert providers.openai_model_for(BY_KEY["pm"]) == "MiniMax-M3"  # pm ∈ LEAD_ROLES
    assert providers.openai_model_for(BY_KEY["engineer"]) == "MiniMax-M2.7"


def test_make_expert_minimax(monkeypatch, tmp_path):
    """minimax 與 openai 共用 OpenAIExpert（function-calling 工具迴圈）。"""
    monkeypatch.setattr(config, "PROVIDER", "minimax")
    ex = providers.make_expert(BY_KEY["engineer"], "t", tmp_path)
    assert isinstance(ex, providers.OpenAIExpert)


def test_codex_argv_uses_exec_json_and_role_sandbox(monkeypatch, tmp_path):
    """codex provider 以 argv 呼叫非互動 JSONL；可寫角色才給 workspace-write。"""
    monkeypatch.setattr(config, "CODEX_BIN", "codex")
    monkeypatch.setattr(config, "CODEX_MODEL_FAST", "")
    monkeypatch.setattr(config, "CODEX_SANDBOX", "auto")
    monkeypatch.setattr(config, "CODEX_BYPASS_SANDBOX", False)
    engineer = providers._codex_argv(BY_KEY["engineer"], tmp_path)
    pm = providers._codex_argv(BY_KEY["pm"], tmp_path)

    assert engineer[:3] == ["codex", "exec", "--json"]
    assert "--ephemeral" in engineer
    assert engineer[-1] == "-"
    assert engineer[engineer.index("-c") + 1] == 'approval_policy="never"'
    assert engineer[engineer.index("--cd") + 1] == str(tmp_path)
    assert engineer[engineer.index("--sandbox") + 1] == "workspace-write"
    assert pm[pm.index("--sandbox") + 1] == "read-only"


def test_codex_argv_allows_danger_full_access(monkeypatch, tmp_path):
    """TI_CODEX_SANDBOX 可明確覆寫成 Codex CLI 的 danger-full-access。"""
    monkeypatch.setattr(config, "CODEX_MODEL_FAST", "")
    monkeypatch.setattr(config, "CODEX_SANDBOX", "danger-full-access")
    monkeypatch.setattr(config, "CODEX_BYPASS_SANDBOX", False)

    argv = providers._codex_argv(BY_KEY["pm"], tmp_path)

    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert argv[argv.index("-c") + 1] == 'approval_policy="never"'
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_argv_can_bypass_sandbox(monkeypatch, tmp_path):
    """TI_CODEX_BYPASS_SANDBOX=1 時使用 Codex CLI 的完整 bypass 旗標。"""
    monkeypatch.setattr(config, "CODEX_MODEL_FAST", "")
    monkeypatch.setattr(config, "CODEX_SANDBOX", "auto")
    monkeypatch.setattr(config, "CODEX_BYPASS_SANDBOX", True)

    argv = providers._codex_argv(BY_KEY["engineer"], tmp_path)

    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--sandbox" not in argv
    assert "-c" not in argv


def test_codex_argv_adds_model_but_not_unsupported_search_flag(monkeypatch, tmp_path):
    """Codex 模型有設定才覆寫；不傳目前 codex exec 不支援的 --search。"""
    monkeypatch.setattr(config, "CODEX_MODEL_FAST", "gpt-test-fast")
    monkeypatch.setattr(config, "CODEX_SANDBOX", "auto")
    monkeypatch.setattr(config, "CODEX_BYPASS_SANDBOX", False)
    argv = providers._codex_argv(BY_KEY["researcher"], tmp_path)
    assert argv[argv.index("--model") + 1] == "gpt-test-fast"
    assert "--search" not in argv


def test_make_expert_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROVIDER", "codex")
    ex = providers.make_expert(BY_KEY["engineer"], "t", tmp_path)
    assert isinstance(ex, providers.CodexExpert)


def test_provider_ready_codex(monkeypatch, tmp_path):
    """codex provider 需要 CLI 可執行，且有 CODEX_API_KEY 或 CODEX_HOME/auth.json。"""
    monkeypatch.setattr(config, "PROVIDER", "codex")
    monkeypatch.setattr(config, "CODEX_BIN", "codex")
    monkeypatch.setattr(config, "CODEX_API_KEY", "")
    monkeypatch.setattr(config, "CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/codex")

    assert config.provider_ready() is False
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    assert config.provider_ready() is True
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config.provider_ready() is False
    monkeypatch.setattr(config, "CODEX_API_KEY", "ck-test")
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/codex")
    assert config.provider_ready() is True


@pytest.mark.asyncio
async def test_codex_event_mapping(tmp_path):
    """Codex JSONL item 轉成現有工具/訊息事件，不需真的啟動 codex。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()

    await expert._handle_codex_event(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "python3 -m pytest -q\n"},
        },
        broadcast,
    )
    text = await expert._handle_codex_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "完成"}},
        broadcast,
    )

    assert text == "完成"
    assert any(e.type == events.EventType.TOOL_USE for e in bucket)
    assert any(e.type == events.EventType.EXPERT_MESSAGE for e in bucket)


def test_openai_client_args_minimax(monkeypatch):
    """PROVIDER=minimax 時用 MiniMax 的 key/base_url，與 OpenAI 憑證互不污染。"""
    monkeypatch.setattr(config, "PROVIDER", "minimax")
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "mm-key")
    monkeypatch.setattr(config, "MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "should-not-be-used")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://should-not-be-used")
    assert providers._openai_client_args() == ("mm-key", "https://api.minimax.io/v1")


def test_openai_client_args_openai(monkeypatch):
    """PROVIDER=openai（及非 minimax 預設）走 OpenAI 憑證。"""
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "oa-key")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://localhost:11434/v1")
    assert providers._openai_client_args() == ("oa-key", "http://localhost:11434/v1")


def test_effective_provider_override(monkeypatch):
    """per-role 覆寫優先於全域；無覆寫沿用全域。"""
    monkeypatch.setattr(config, "PROVIDER", "claude")
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {"engineer": "claude", "pm": "minimax"})
    assert providers.effective_provider(BY_KEY["pm"]) == "minimax"  # 覆寫
    assert providers.effective_provider(BY_KEY["engineer"]) == "claude"  # 覆寫
    assert providers.effective_provider(BY_KEY["qa"]) == "claude"  # 無覆寫→全域


def test_make_expert_mixed_per_role(monkeypatch, tmp_path):
    """混用：全域 claude，但把 pm 覆寫成 minimax → pm 走 OpenAIExpert、其餘走 Claude Expert。"""
    monkeypatch.setattr(config, "PROVIDER", "claude")
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {"pm": "minimax"})
    pm = providers.make_expert(BY_KEY["pm"], "t", tmp_path)
    assert isinstance(pm, providers.OpenAIExpert)
    # 非覆寫角色走 Claude 路徑（Expert，延後 import）。
    from studio import experts
    from studio.experts import Expert

    # monkeypatch _build_client：不需真 SDK、不連線即可驗證型別分派。
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: object())
    qa = providers.make_expert(BY_KEY["qa"], "t", tmp_path)
    assert isinstance(qa, Expert)


def test_make_expert_mixed_model_slot(monkeypatch):
    """混用時模型槽依角色有效 provider 決定，不被全域帶歪。"""
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {"pm": "minimax"})
    monkeypatch.setattr(config, "MINIMAX_MODEL_LEAD", "MiniMax-M3")
    # pm ∈ LEAD_ROLES 且覆寫 minimax → MiniMax 主力模型
    assert providers.openai_model_for(BY_KEY["pm"]) == "MiniMax-M3"
    # engineer 無覆寫 → 全域 openai 快速模型
    assert providers.openai_model_for(BY_KEY["engineer"]) == config.OPENAI_MODEL_FAST


def test_provider_ready_minimax(monkeypatch):
    """minimax 只認 API key（base_url 有預設端點）。"""
    monkeypatch.setattr(config, "PROVIDER", "minimax")
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
    assert config.provider_ready() is False
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "mm-key")
    assert config.provider_ready() is True


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

    async def exploding_chat(messages, tools_, model, **_kw):
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

    async def exploding_chat(messages, tools_, model, **_kw):
        called["n"] += 1
        raise ValueError("非預期型別")

    monkeypatch.setattr(providers, "_openai_chat", exploding_chat)

    out = await providers.complete_once(
        "你是反思器", "請反思", session_id="s", cwd=tmp_path, timeout=1.0
    )

    assert out == ""
    assert called["n"] == 1
