"""QA 驗收：任務 #5「orchestrator(_self_test/_final_demo) 與 tools(run_bash) 保留 shell」。

對應驗收標準 #4：
- orchestrator.py(718/735)、tools.py(131) 仍走 shell 版 run_command（非 run_command_exec）。
- 加上註解說明為何保留 shell（使用者/PM 動態指令需 shell 語法）。
- 行為與遷移前一致——即 shell 仍會解析 ;/&&/$()/pipe 等 metacharacter。

策略：
- 原始碼層級：三處呼叫端使用 run_command(（非 _exec）、帶 # nosec B602 與「保留 shell」說明。
- 行為層級：以真實 workspace 驅動 _self_test / _final_demo / run_bash，傳入含 &&/$()/pipe
  的動態指令，證明 shell 語法仍被解析（行為未變）。
- 路徑層級：spy 確認這三處走 shell run_command、未誤切到 run_command_exec。
"""

from __future__ import annotations

import inspect
import re

import pytest

from studio import runner, tools
from studio.orchestrator import StudioSession
from studio.runner import RunOutput


def _collect():
    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


# ---------------------------------------------------------------------------
# 驗收 #4：原始碼層級——保留 shell + 註解 + nosec
# ---------------------------------------------------------------------------
def _assert_shell_kept(src: str, who: str):
    # 仍使用 shell 版 run_command（排除 run_command_exec）。
    shell_hits = re.findall(r"(?<!_exec)\brun_command\(", src)
    assert shell_hits, f"{who} 應保留 shell 版 run_command"
    # 不得改用 exec。
    assert "run_command_exec(" not in src, f"{who} 不應改用 run_command_exec"
    # 有 nosec 標註避免 CI 誤報。
    assert "# nosec" in src, f"{who} 缺 # nosec 標註"
    # 有「保留 shell」的說明性註解。
    assert "保留 shell" in src or "shell 語法" in src, f"{who} 缺保留 shell 的說明註解"


def test_source_orchestrator_self_test_keeps_shell():
    _assert_shell_kept(inspect.getsource(StudioSession._self_test), "_self_test")


def test_source_orchestrator_final_demo_keeps_shell():
    _assert_shell_kept(inspect.getsource(StudioSession._final_demo), "_final_demo")


def test_source_tools_run_bash_keeps_shell():
    # run_bash 分支在 tools.execute 內，取整個 execute 原始碼檢視。
    src = inspect.getsource(tools.execute)
    assert 'name == "run_bash"' in src
    _assert_shell_kept(src, "run_bash")


# ---------------------------------------------------------------------------
# 驗收 #4：路徑層級——確實走 shell run_command，未誤切 exec
# ---------------------------------------------------------------------------
class ShellSpy:
    def __init__(self):
        self.calls = []

    async def __call__(self, cwd, command, timeout=None, sandbox=None):
        self.calls.append({"cwd": str(cwd), "command": command})
        return RunOutput(command=command, exit_code=0, output="ok", timed_out=False)


async def _boom_exec(*a, **k):
    raise AssertionError("保留 shell 的呼叫端不應走 run_command_exec")


@pytest.mark.asyncio
async def test_self_test_routes_to_shell_run_command(monkeypatch, tmp_path):
    spy = ShellSpy()
    monkeypatch.setattr(runner, "run_command", spy)
    monkeypatch.setattr(runner, "run_command_exec", _boom_exec)

    _, broadcast = _collect()
    session = StudioSession("t", broadcast, cwd=tmp_path)
    await session._self_test("執行指令: echo A && echo B")

    assert len(spy.calls) == 1
    # 動態指令原樣（含 shell 語法）交給 shell run_command，未被拆解或改寫。
    assert spy.calls[0]["command"] == "echo A && echo B"


@pytest.mark.asyncio
async def test_final_demo_routes_to_shell_run_command(monkeypatch, tmp_path):
    spy = ShellSpy()
    monkeypatch.setattr(runner, "run_command", spy)
    monkeypatch.setattr(runner, "run_command_exec", _boom_exec)

    _, broadcast = _collect()
    session = StudioSession("t", broadcast, cwd=tmp_path)
    session._run_command = "echo $(echo X) | cat"
    await session._final_demo()

    assert len(spy.calls) == 1
    assert spy.calls[0]["command"] == "echo $(echo X) | cat"


@pytest.mark.asyncio
async def test_run_bash_routes_to_shell_run_command(monkeypatch, tmp_path):
    spy = ShellSpy()
    monkeypatch.setattr(runner, "run_command", spy)
    monkeypatch.setattr(runner, "run_command_exec", _boom_exec)

    out = await tools.execute("run_bash", {"command": "echo hi && ls | wc -l"}, tmp_path)
    assert "exit=0" in out
    assert len(spy.calls) == 1
    assert spy.calls[0]["command"] == "echo hi && ls | wc -l"


# ---------------------------------------------------------------------------
# 驗收 #4：行為層級——真實 shell 仍解析 metacharacter（行為與遷移前一致）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_bash_shell_still_interprets_metachars(tmp_path):
    """run_bash 真跑：&& / $() / pipe 仍被 shell 解析（這正是保留 shell 的目的）。"""
    out = await tools.execute(
        "run_bash", {"command": "echo first && echo $(echo second) | cat"}, tmp_path
    )
    assert "first" in out and "second" in out, f"shell 語法未被解析：{out!r}"


@pytest.mark.asyncio
async def test_self_test_shell_executes_chained_command(tmp_path):
    """_self_test 真跑動態鏈式指令：&& 串接被 shell 執行，輸出含兩段。"""
    bucket, broadcast = _collect()
    session = StudioSession("t", broadcast, cwd=tmp_path)
    await session._self_test("執行指令: echo ALPHA && echo BETA")
    # 從廣播事件 payload 取出回報的 log，確認 shell 串接生效。
    logs = " ".join(ev.payload.get("log", "") or "" for ev in bucket)
    assert "ALPHA" in logs and "BETA" in logs, f"shell && 未被執行：{logs!r}"


@pytest.mark.asyncio
async def test_final_demo_shell_executes_substitution(tmp_path):
    """_final_demo 真跑：$() 命令替換被 shell 解析。"""
    bucket, broadcast = _collect()
    session = StudioSession("t", broadcast, cwd=tmp_path)
    session._run_command = "echo VAL=$(echo 42)"
    await session._final_demo()
    # demo_result 事件 payload 帶 output。
    outputs = " ".join(ev.payload.get("output", "") or "" for ev in bucket)
    assert "VAL=42" in outputs, f"shell $() 未被解析：{outputs!r}"
