"""測試 OpenAI provider 的工具迴圈（以 fake chat 取代真實 API）。"""

from __future__ import annotations

import ast
import asyncio
import tomllib
from pathlib import Path
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


class FakePipe:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def read(self, _size=-1):
        return b""


class LinesPipe(FakePipe):
    def __init__(self, lines):
        super().__init__()
        self._lines = [line.encode("utf-8") for line in lines]

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class BytesPipe(FakePipe):
    def __init__(self, chunks):
        super().__init__()
        self._chunks = list(chunks)

    async def read(self, _size=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeCodexProcess:
    def __init__(self):
        self.pid = 12345
        self.returncode = None
        self.stdin = FakePipe()
        self.stdout = FakePipe()
        self.stderr = FakePipe()
        self._done = asyncio.Event()
        self.killed = False
        self.wait_calls = 0

    async def wait(self):
        self.wait_calls += 1
        await self._done.wait()
        return self.returncode

    def finish(self, returncode=0):
        self.returncode = returncode
        self._done.set()

    def kill(self):
        self.killed = True
        self.finish(-9)


class FailingDrainPipe(FakePipe):
    async def drain(self):
        raise RuntimeError("stdin failed")


class SwappingFailingDrainPipe(FakePipe):
    def __init__(self, expert, newer_proc):
        super().__init__()
        self.expert = expert
        self.newer_proc = newer_proc

    async def drain(self):
        self.expert._proc = self.newer_proc
        raise RuntimeError("stdin failed after proc swap")


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


def test_openai_model_for_gemini(monkeypatch):
    """PROVIDER=gemini 時走 Gemini 模型槽（依 LEAD_ROLES 二分）。"""
    monkeypatch.setattr(config, "PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_MODEL_LEAD", "gemini-2.5-pro")
    monkeypatch.setattr(config, "GEMINI_MODEL_FAST", "gemini-2.5-flash")
    assert providers.openai_model_for(BY_KEY["pm"]) == "gemini-2.5-pro"
    assert providers.openai_model_for(BY_KEY["engineer"]) == "gemini-2.5-flash"


def test_make_expert_minimax(monkeypatch, tmp_path):
    """minimax 與 openai 共用 OpenAIExpert（function-calling 工具迴圈）。"""
    monkeypatch.setattr(config, "PROVIDER", "minimax")
    ex = providers.make_expert(BY_KEY["engineer"], "t", tmp_path)
    assert isinstance(ex, providers.OpenAIExpert)


def test_make_expert_gemini(monkeypatch, tmp_path):
    """gemini 走 OpenAI 相容工具迴圈，但使用 Gemini 憑證/模型槽。"""
    monkeypatch.setattr(config, "PROVIDER", "gemini")
    ex = providers.make_expert(BY_KEY["engineer"], "t", tmp_path)
    assert isinstance(ex, providers.OpenAIExpert)
    assert ex._provider == "gemini"


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


def test_antigravity_model_for(monkeypatch):
    """Antigravity 模型槽同樣依 LEAD_ROLES 二分，留空則沿用 CLI 設定。"""
    monkeypatch.setattr(config, "ANTIGRAVITY_MODEL_LEAD", "Gemini 3.5 Flash (High)")
    monkeypatch.setattr(config, "ANTIGRAVITY_MODEL_FAST", "Gemini 3.5 Flash (Low)")

    assert providers.antigravity_model_for(BY_KEY["pm"]) == "Gemini 3.5 Flash (High)"
    assert providers.antigravity_model_for(BY_KEY["engineer"]) == "Gemini 3.5 Flash (Low)"


def test_antigravity_argv_uses_print_model_sandbox_and_timeout(monkeypatch):
    """agy provider 走 print mode，帶模型、sandbox、auto-approve 與現有發言 timeout。"""
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "agy")
    monkeypatch.setattr(config, "ANTIGRAVITY_MODEL_FAST", "Gemini 3.5 Flash (Low)")
    monkeypatch.setattr(config, "ANTIGRAVITY_SANDBOX", True)
    monkeypatch.setattr(config, "ANTIGRAVITY_SKIP_PERMISSIONS", True)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 17)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 99)

    argv = providers._antigravity_argv(BY_KEY["engineer"])

    assert argv[:3] == ["agy", "--sandbox", "--dangerously-skip-permissions"]
    assert argv[argv.index("--model") + 1] == "Gemini 3.5 Flash (Low)"
    assert argv[argv.index("--print-timeout") + 1] == "17s"
    assert argv[-1] == "-p"


def test_antigravity_argv_can_disable_sandbox_or_permissions(monkeypatch):
    """Antigravity sandbox/permission 旗標可各自關閉，方便沿用外部部署限制。"""
    monkeypatch.setattr(config, "ANTIGRAVITY_MODEL_FAST", "")
    monkeypatch.setattr(config, "ANTIGRAVITY_SANDBOX", False)
    monkeypatch.setattr(config, "ANTIGRAVITY_SKIP_PERMISSIONS", False)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)

    argv = providers._antigravity_argv(BY_KEY["engineer"])

    assert "--sandbox" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--model" not in argv
    assert "--print-timeout" not in argv


def test_make_expert_antigravity(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROVIDER", "antigravity")
    ex = providers.make_expert(BY_KEY["engineer"], "t", tmp_path)
    assert isinstance(ex, providers.AntigravityExpert)


@pytest.mark.asyncio
async def test_antigravity_success_broadcasts_stdout(monkeypatch, tmp_path):
    """agy stdout 會被收斂為一般專家訊息。"""
    expert = providers.AntigravityExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    proc.stdout = LinesPipe(["完成\n"])

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        proc.finish(0)
        return proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    out = await expert._run_antigravity("請回覆", broadcast)

    assert out == "完成"
    assert any(
        e.type == events.EventType.EXPERT_MESSAGE and e.payload.get("text") == "完成"
        for e in bucket
    )


@pytest.mark.asyncio
async def test_antigravity_auth_required_raises_provider_unavailable(monkeypatch, tmp_path):
    """agy 未登入時不能被包成普通專家訊息，應暫停 provider 等使用者登入。"""
    expert = providers.AntigravityExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    proc.stdout = LinesPipe(["Authentication required. Please sign in to continue.\n"])

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        proc.finish(1)
        return proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(providers.ProviderUnavailable) as seen:
        await expert._run_antigravity("請回覆", broadcast)

    assert seen.value.provider == "antigravity"
    assert "Authentication required" in seen.value.detail
    assert bucket == []


def test_antigravity_unavailable_delegates_to_llm_caller(monkeypatch):
    """Antigravity 不應保留本地 auth phrase 白名單；核心回 None 就不得自行判 unavailable。"""
    calls = []

    def fake_reason(text):
        calls.append(text)
        return None

    monkeypatch.setattr(providers.llm_caller, "provider_unavailable_reason", fake_reason)

    assert providers._antigravity_unavailable("Authentication required. Please sign in.") is False
    assert calls == ["Authentication required. Please sign in."]


@pytest.mark.asyncio
async def test_codex_run_saves_current_proc_after_spawn(monkeypatch, tmp_path):
    """_run_codex 建立 subprocess 後要立刻保存，讓 stop() 能找到目前 proc。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    created = asyncio.Event()
    spawn_kwargs = {}

    async def fake_create_subprocess_exec(*_args, **kwargs):
        spawn_kwargs.update(kwargs)
        created.set()
        return proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    task = asyncio.create_task(expert._run_codex("請回覆", broadcast))
    try:
        await asyncio.wait_for(created.wait(), timeout=1)
        await asyncio.sleep(0)
        assert getattr(expert, "_proc", None) is proc
        assert spawn_kwargs["start_new_session"] is True
    finally:
        proc.finish()
        await asyncio.wait_for(task, timeout=1)

    assert expert._proc is None
    assert bucket == []


@pytest.mark.asyncio
async def test_codex_stop_calls_terminate_for_running_proc(monkeypatch, tmp_path):
    """stop() 對執行中的 proc 必須委派給 _terminate()，並等待 proc 回收。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    proc = FakeCodexProcess()
    expert._proc = proc
    calls = []

    def fake_terminate(seen):
        calls.append(seen)
        seen.finish(-15)

    monkeypatch.setattr(expert, "_terminate", fake_terminate)

    await expert.stop()

    assert calls == [proc]
    assert proc.wait_calls == 1
    assert expert._proc is None


@pytest.mark.asyncio
async def test_codex_stop_is_idempotent(monkeypatch, tmp_path):
    """重複 stop() 同一個 proc 不應重複送終止訊號。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    proc = FakeCodexProcess()
    expert._proc = proc
    terminated = asyncio.Event()
    calls = []

    def fake_terminate(seen):
        calls.append(seen)
        terminated.set()

    monkeypatch.setattr(expert, "_terminate", fake_terminate)

    first_stop = asyncio.create_task(expert.stop())
    await asyncio.wait_for(terminated.wait(), timeout=1)
    await asyncio.sleep(0)

    assert calls == [proc]
    assert not first_stop.done()
    assert expert._proc is proc

    second_stop = asyncio.create_task(expert.stop())
    await asyncio.sleep(0)

    assert calls == [proc]
    assert not second_stop.done()
    assert expert._proc is proc

    proc.finish(-15)
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), timeout=1)

    assert calls == [proc]
    assert proc.wait_calls == 1
    assert expert._proc is None


@pytest.mark.asyncio
async def test_codex_stop_ignores_missing_or_finished_proc(monkeypatch, tmp_path):
    """沒有 proc 或 proc 已結束時，stop() 不應送終止訊號。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    calls = []

    monkeypatch.setattr(expert, "_terminate", lambda seen: calls.append(seen))

    await expert.stop()
    proc = FakeCodexProcess()
    proc.finish(0)
    expert._proc = proc
    await expert.stop()

    assert calls == []


@pytest.mark.asyncio
async def test_codex_run_finally_does_not_clear_newer_proc(monkeypatch, tmp_path):
    """舊輪 _run_codex 收尾時，不可誤清下一輪已保存的新 proc。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()
    old_proc = FakeCodexProcess()
    newer_proc = FakeCodexProcess()
    created = asyncio.Event()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        created.set()
        return old_proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    task = asyncio.create_task(expert._run_codex("請回覆", broadcast))
    try:
        await asyncio.wait_for(created.wait(), timeout=1)
        await asyncio.sleep(0)
        assert expert._proc is old_proc
        expert._proc = newer_proc
    finally:
        old_proc.finish()
        await asyncio.wait_for(task, timeout=1)

    assert expert._proc is newer_proc
    assert bucket == []


@pytest.mark.asyncio
async def test_codex_run_finally_clears_current_proc_on_error(monkeypatch, tmp_path):
    """_run_codex 異常離開時，finally 仍要清掉同一個 proc 引用。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    proc.stdin = FailingDrainPipe()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="stdin failed"):
        await expert._run_codex("請回覆", broadcast)

    assert expert._proc is None
    assert proc.returncode is None
    assert bucket == []


@pytest.mark.asyncio
async def test_codex_run_finally_does_not_clear_newer_proc_on_error(monkeypatch, tmp_path):
    """舊輪 _run_codex 拋錯離開時，也不可誤清已換上的新 proc。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()
    old_proc = FakeCodexProcess()
    newer_proc = FakeCodexProcess()
    old_proc.stdin = SwappingFailingDrainPipe(expert, newer_proc)

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return old_proc

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="stdin failed after proc swap"):
        await expert._run_codex("請回覆", broadcast)

    assert expert._proc is newer_proc
    assert old_proc.returncode is None
    assert bucket == []


@pytest.mark.asyncio
async def test_codex_run_cancel_reaps_group_and_waits_proc(monkeypatch, tmp_path):
    """_run_codex() 被取消時，不能只清參照，必須對整組收屍（reap_group 用記下的 pgid，撐得過
    leader 已被 reap）並等待目前 proc——取代會在 leader 被 reap 後失效的 _terminate/getpgid。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    created = asyncio.Event()
    reaped = asyncio.Event()
    reap_calls = []

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        created.set()
        return proc

    def fake_reap_group(pgid):
        reap_calls.append(pgid)
        reaped.set()

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(providers.runner, "reap_group", fake_reap_group)

    task = asyncio.create_task(expert._run_codex("請回覆", broadcast))
    await asyncio.wait_for(created.wait(), timeout=1)
    await asyncio.sleep(0)
    assert expert._proc is proc

    task.cancel()
    await asyncio.wait_for(reaped.wait(), timeout=1)
    await asyncio.sleep(0)

    # 用記下的 pgid（==spawn 當下的 pid）整組收屍，而非依賴 leader 仍在的 getpgid。
    assert reap_calls == [proc.pid]
    assert not task.done()
    assert expert._proc is proc

    proc.finish(-15)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert proc.wait_calls >= 1
    assert expert._proc is None
    assert bucket == []


def test_codex_terminate_prefers_process_group(monkeypatch, tmp_path):
    """_terminate() 優先殺 process group，避免只殺直屬 child。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    proc = FakeCodexProcess()
    calls = []

    monkeypatch.setattr(providers.runner.os, "getpgid", lambda pid: 67890)
    monkeypatch.setattr(
        providers.runner.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
    )

    expert._terminate(proc)

    assert calls == [(67890, providers.runner.signal.SIGKILL)]
    assert proc.killed is False


def test_codex_terminate_uses_existing_process_group_helper(monkeypatch, tmp_path):
    """CodexExpert 不自行分叉終止邏輯，應沿用 runner 既有 process-group helper。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    proc = FakeCodexProcess()
    calls = []

    monkeypatch.setattr(providers.runner, "kill_process_group", lambda seen: calls.append(seen))

    expert._terminate(proc)

    assert calls == [proc]


def test_codex_terminate_falls_back_to_direct_kill(monkeypatch, tmp_path):
    """process group 取不到或殺不到時，退回標準庫 proc.kill()。"""
    expert = providers.CodexExpert(BY_KEY["engineer"], "t", tmp_path)
    proc = FakeCodexProcess()

    monkeypatch.setattr(providers.runner.os, "getpgid", lambda pid: 67890)
    monkeypatch.setattr(
        providers.runner.os,
        "killpg",
        lambda _pgid, _sig: (_ for _ in ()).throw(OSError("gone")),
    )

    expert._terminate(proc)

    assert proc.killed is True
    assert proc.returncode == -9


def test_codex_process_lifecycle_does_not_add_psutil_dependency():
    """任務 #5 要求保留標準庫方案，不應新增 psutil 依賴或 import。"""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = list(pyproject["project"]["dependencies"])
    for group in pyproject["project"].get("optional-dependencies", {}).values():
        dependencies.extend(group)

    assert all(not dep.lower().startswith("psutil") for dep in dependencies)
    assert not _imports_module(Path("studio/providers.py"), "psutil")
    assert not _imports_module(Path("studio/runner.py"), "psutil")


@pytest.mark.asyncio
async def test_codex_usage_limit_raises_provider_unavailable(monkeypatch, tmp_path):
    """Codex usage limit 不能被包成普通專家訊息，否則會被 QA 誤記成 qa_fail。"""
    expert = providers.CodexExpert(BY_KEY["qa"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    proc.stdout = LinesPipe(
        [
            '{"type":"turn.failed","error":{"message":"You\\u0027ve hit your usage limit. '
            'Visit https://chatgpt.com/codex/settings/usage to purchase more credits."}}\n'
        ]
    )

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        proc.finish(1)
        return proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(providers.ProviderUnavailable) as seen:
        await expert._run_codex("請驗證", broadcast)

    assert seen.value.provider == "codex"
    assert bucket == []


@pytest.mark.asyncio
async def test_codex_jsonl_error_nonzero_exit_not_hidden_by_stderr(monkeypatch, tmp_path):
    """非零 exit 時 stderr 雜訊不能遮蔽 JSONL error，仍須進核心不可用分類。"""
    expert = providers.CodexExpert(BY_KEY["qa"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    proc.stdout = LinesPipe(
        [
            '{"type":"turn.failed","error":{"message":"You\\u0027ve hit your usage limit. '
            'Visit https://chatgpt.com/codex/settings/usage to purchase more credits."}}\n'
        ]
    )
    proc.stderr = BytesPipe([b"debug: harmless stderr noise\n"])

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        proc.finish(1)
        return proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(providers.ProviderUnavailable) as seen:
        await expert._run_codex("請驗證", broadcast)

    assert seen.value.provider == "codex"
    assert "usage limit" in seen.value.detail
    assert "harmless stderr noise" in seen.value.detail
    assert bucket == []


@pytest.mark.asyncio
async def test_codex_jsonl_error_zero_exit_uses_core_unavailable_classification(
    monkeypatch, tmp_path
):
    """Codex JSONL error 即使 exit 0，也要先進核心不可用分類，不能包成普通專家訊息。"""
    expert = providers.CodexExpert(BY_KEY["qa"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    proc.stdout = LinesPipe(
        [
            '{"type":"turn.failed","error":{"message":"You\\u0027ve hit your usage limit. '
            'Visit https://chatgpt.com/codex/settings/usage to purchase more credits."}}\n'
        ]
    )

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        proc.finish(0)
        return proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(providers.ProviderUnavailable) as seen:
        await expert._run_codex("請驗證", broadcast)

    assert seen.value.provider == "codex"
    assert bucket == []


@pytest.mark.asyncio
async def test_codex_jsonl_rate_limit_zero_exit_is_soft_note(monkeypatch, tmp_path):
    """Codex JSONL 暫態 rate limit 由核心分類後維持本輪 soft note，不暫停整個 provider。"""
    expert = providers.CodexExpert(BY_KEY["qa"], "t", tmp_path)
    bucket, broadcast = collect()
    proc = FakeCodexProcess()
    proc.stdout = LinesPipe(
        ['{"type":"turn.failed","error":{"message":"Error code: 429 - slow down"}}\n']
    )

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        proc.finish(0)
        return proc

    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0)
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    out = await expert._run_codex("請驗證", broadcast)

    assert out.startswith("【系統】Codex 本輪暫時不可用")
    assert "429" in out
    assert any(e.type == events.EventType.EXPERT_MESSAGE for e in bucket)


def _imports_module(path: Path, module_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == module_name or alias.name.startswith(f"{module_name}.")
                for alias in node.names
            ):
                return True
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == module_name or node.module.startswith(f"{module_name}."):
                return True
    return False


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


def test_openai_client_args_gemini(monkeypatch):
    """PROVIDER=gemini 時用 Gemini 的 key/base_url，與 OpenAI/MiniMax 憑證互不污染。"""
    monkeypatch.setattr(config, "PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gm-key")
    monkeypatch.setattr(
        config, "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    monkeypatch.setattr(config, "OPENAI_API_KEY", "should-not-be-used")
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "should-not-be-used")
    assert providers._openai_client_args() == (
        "gm-key",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )


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


def test_provider_ready_gemini(monkeypatch):
    """gemini 只認 API key；base_url 有官方預設端點。"""
    monkeypatch.setattr(config, "PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert config.provider_ready() is False
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gm-key")
    assert config.provider_ready() is True


def test_provider_ready_antigravity(monkeypatch):
    """antigravity readiness 只檢查 agy 是否可執行；OAuth/quota 由 CLI 執行時判斷。"""
    monkeypatch.setattr(config, "PROVIDER", "antigravity")
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "agy")
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/agy")
    assert config.provider_ready() is True
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config.provider_ready() is False


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


# --- CodexExpert 整合：真實 fake-codex 腳本，證明 reap 解掉孫程序握 pipe 的卡死 ----------
# 對應 senior/engineer 整輪卡到外層 3600s 的真因：codex 主程序退出但 --sandbox 工具孫程序
# 仍握著 stdout/stderr pipe → reader async-for 永不 EOF、無上限的 await proc.wait() 假死。
# 修法（移植自 AntigravityExpert PR #212）：所有 teardown 路徑先 runner.reap_group(pgid)。


def _write_fake_codex(tmp_path, body: str) -> str:
    p = tmp_path / "fake_codex.sh"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def _codex_env(monkeypatch):
    monkeypatch.setattr(config, "CODEX_MODEL_LEAD", "")
    monkeypatch.setattr(config, "CODEX_MODEL_FAST", "")
    monkeypatch.setattr(config, "CODEX_SANDBOX", "danger-full-access")
    monkeypatch.setattr(config, "CODEX_BYPASS_SANDBOX", False)
    monkeypatch.setattr(config, "CODEX_HOME", "")


@pytest.mark.asyncio
async def test_codex_normal_exit_with_leaked_grandchild_reaped_not_hung(
    monkeypatch, tmp_path, _codex_env
):
    """codex 退出但背景孫程序握著 stdout pipe：reap 後須迅速回傳，而非卡在 async-for。"""
    # 放大 join 上限：唯一能讓它「快速」回傳的，就是 reap_group 收掉孫程序讓 pipe EOF。
    monkeypatch.setattr(providers, "_READER_JOIN_TIMEOUT", 30.0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 30.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 30.0)
    codex = _write_fake_codex(
        tmp_path,
        "cat >/dev/null\n"
        "printf '%s\\n' "
        '\'{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"審查意見：本輪無阻擋項目。"}}\'\n'
        "sleep 30 &\nexit 0\n",
    )
    monkeypatch.setattr(config, "CODEX_BIN", codex)

    expert = providers.CodexExpert(BY_KEY["senior"], "sess", tmp_path)
    bucket, broadcast = collect()

    # 若 reap 失效，會卡到 30s join 上限；timeout=5 證明確實是 reap 讓它秒回。
    text = await asyncio.wait_for(expert.speak("審查任務", broadcast), timeout=5)
    assert "本輪無阻擋項目" in text


@pytest.mark.asyncio
async def test_codex_turn_timeout_soft_fails_without_pause(monkeypatch, tmp_path, _codex_env):
    """codex 整輪無輸出卡住：watchdog 逾時須 reap 收屍並回系統 note 軟失敗，不卡到外層 timeout。"""
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.5)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)
    codex = _write_fake_codex(tmp_path, "cat >/dev/null\nsleep 30\n")  # 讀完 prompt 後無輸出卡住
    monkeypatch.setattr(config, "CODEX_BIN", codex)

    expert = providers.CodexExpert(BY_KEY["senior"], "sess", tmp_path)
    bucket, broadcast = collect()

    result = await asyncio.wait_for(expert.speak("審查任務", broadcast), timeout=8)

    # 軟失敗：回系統 note（含「逾時」），略過本輪而非拋例外或卡死整場。
    assert result.startswith("【系統】") and "逾時" in result
    # 最後狀態回 idle（speak 的 finally 有廣播）。
    assert bucket[-1].payload["status"] == "idle"
