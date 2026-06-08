"""QA 驗收：任務 #3「autopilot.py 的 `python -m pytest -q` 遷移為 run_command_exec」。

對應驗收標準：
- #2：autopilot 的 pytest 改用 run_command_exec（argv list），原始檔不再以 shell 字串呼叫。
- #5：含 `;`/`&&`/`$()` 的路徑/參數被當純文字（exec 不經 shell），不發生注入。
- 設計決策：argv 固定 ["python","-m","pytest","-q"]、timeout=600、sandbox=True；
  並在 sandbox（bwrap）下實跑驗證 PATH 能解析到 python。

策略：
- spy 攔截 run_command_exec → 檢查 argv/timeout/sandbox/label，並確認 shell 版
  run_command 完全沒被呼叫。
- _gate_tests 回傳值（ok, output[-1500:]）行為驗證。
- sandbox 下真實 exec 探針，證明 bwrap 能解析 python（避免 PATH 問題）。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path

import pytest

from studio import autopilot, config, runner
from studio.runner import RunOutput

STUDIO = Path(__file__).resolve().parent.parent / "studio"


@pytest.fixture(autouse=True)
def _no_shell(monkeypatch):
    """保險絲：_gate_tests 不得開出 shell 子程序。"""

    async def _boom_shell(*a, **k):
        raise AssertionError("不應呼叫 create_subprocess_shell：pytest gate 必須走 exec")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom_shell)


class ExecSpy:
    def __init__(self, output: str = "", exit_code: int = 0):
        self.calls: list[dict] = []
        self._output = output
        self._exit = exit_code

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None):
        self.calls.append(
            {"cwd": cwd, "argv": list(argv), "timeout": timeout, "sandbox": sandbox, "label": label}
        )
        return RunOutput(
            command=label or (argv[0] if argv else ""),
            exit_code=self._exit,
            output=self._output,
            timed_out=False,
        )


# --- 驗收 #2：argv 化、走 exec ------------------------------------------
async def test_gate_tests_uses_exec_with_fixed_argv(monkeypatch):
    spy = ExecSpy(output="1 passed", exit_code=0)
    monkeypatch.setattr(runner, "run_command_exec", spy)

    async def _boom_rc(*a, **k):
        raise AssertionError("_gate_tests 不應呼叫 shell 版 run_command")

    monkeypatch.setattr(runner, "run_command", _boom_rc)

    ok, out = await autopilot._gate_tests("/some/clone")
    assert ok is True
    assert len(spy.calls) == 1
    call = spy.calls[0]
    # 用 sys.executable（當前直譯器）而非裸 "python"，避免 PATH 無 python 的解析失敗。
    assert call["argv"][0] == sys.executable, call["argv"]
    assert call["argv"][1:] == ["-m", "pytest", "-q"], call["argv"]
    assert call["timeout"] == 600, "須保留 timeout=600"
    assert call["sandbox"] is True, "須顯式 sandbox=True（不可依賴預設）"
    assert call["cwd"] == "/some/clone"


async def test_gate_tests_returns_ok_and_trimmed_output(monkeypatch):
    """ok 由 exit_code 推導；output 截尾 1500 字。"""
    long_out = "x" * 5000 + "TAIL_MARKER"
    spy = ExecSpy(output=long_out, exit_code=0)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_tests("/c")
    assert ok is True
    assert len(out) == 1500
    assert out.endswith("TAIL_MARKER")

    spy_fail = ExecSpy(output="boom", exit_code=1)
    monkeypatch.setattr(runner, "run_command_exec", spy_fail)
    ok2, out2 = await autopilot._gate_tests("/c")
    assert ok2 is False


# --- 驗收 #5：clone 路徑含 metachar 被當純文字（cwd 不經 shell）---------
@pytest.mark.parametrize(
    "evil_clone",
    [
        "/tmp/clone; rm -rf /",
        "/tmp/clone && curl evil",
        "/tmp/clone$(whoami)",
        "/tmp/clone`id`",
    ],
)
async def test_gate_tests_clone_path_is_literal(monkeypatch, evil_clone):
    """argv 固定不變、clone 只當 cwd（exec 不經 shell，metachar 不被解析）。"""
    spy = ExecSpy(output="", exit_code=0)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    await autopilot._gate_tests(evil_clone)
    call = spy.calls[0]
    # 指令 argv 不因 clone 內容改變，且 clone 整串原樣當 cwd。
    assert call["argv"][0] == sys.executable
    assert call["argv"][1:] == ["-m", "pytest", "-q"]
    assert call["cwd"] == evil_clone


# --- 驗收 #2：原始碼層級無 shell run_command ----------------------------
def test_source_no_shell_run_command_in_gate():
    src = inspect.getsource(autopilot._gate_tests)
    shell_hits = re.findall(r"(?<!_exec)\brun_command\(", src)
    assert not shell_hits, f"_gate_tests 仍殘留 shell run_command：{len(shell_hits)} 處"
    assert "run_command_exec(" in src
    # argv 用 sys.executable（避免 PATH 問題）+ 固定 pytest 參數，不再是 shell 字串。
    assert "sys.executable" in src
    assert '"-m", "pytest", "-q"' in src


# --- 設計決策：sandbox 下實跑，驗證 bwrap 能解析 python -----------------
async def test_sandbox_exec_resolves_python():
    """run_command_exec(sandbox=True) 在 bwrap 內能找到並執行 python。"""
    if not config._sandbox_available():
        pytest.skip("環境無 bwrap，跳過 sandbox 實跑")
    r = await runner.run_command_exec(
        "/tmp", [sys.executable, "-c", "print(40 + 2)"], timeout=60, sandbox=True, label="probe"
    )
    assert r.ok, f"sandbox 內 python 無法執行：exit={r.exit_code} out={r.output!r}"
    assert "42" in r.output


async def test_gate_tests_real_sandbox_run(tmp_path):
    """端到端：在 sandbox 內對真實 workspace 跑 _gate_tests，綠燈。"""
    if not config._sandbox_available():
        pytest.skip("環境無 bwrap，跳過 sandbox 實跑")
    (tmp_path / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    ok, out = await autopilot._gate_tests(str(tmp_path))
    assert ok is True, f"sandbox 端到端未通過：{out!r}"
    assert "passed" in out
