"""QA 驗收：任務 #4（autopilot 部分）+ 遷移後呼叫端 metacharacter 純文字總驗。

runner（git_init/git_clone）的 exec 遷移、注入防護與 token 遮蔽驗收見
test_qa_task2_runner_git_exec.py 與 test_clone.py。本檔補齊唯一未覆蓋的遷移呼叫端
——autopilot._gate_tests 的 pytest 閘門——並以端到端方式釘住「遷移後共用的
run_command_exec 對 ;/&&/$()/`` 一律當純文字、不觸發子指令」。

對應驗收標準：
- #2：autopilot 的 pytest 改用 run_command_exec（argv list），不再以 shell 字串呼叫。
- #5（autopilot 部分）：遷移後呼叫端走 exec，含 metacharacter 的參數被當純文字。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys

import pytest

from studio import autopilot, runner
from studio.runner import RunOutput


# ---------------------------------------------------------------------------
# 保險絲：本檔任何測試都不得真的開出 shell 子程序。
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_shell(monkeypatch):
    async def _boom_shell(*a, **k):
        raise AssertionError("autopilot pytest 閘門必須走 exec，不得開 shell")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom_shell)


class ExecSpy:
    """攔截 run_command_exec：記錄 argv/label/sandbox/timeout，回傳可控假輸出。"""

    def __init__(self, output: str = "", ok: bool = True):
        self.calls: list[dict] = []
        self._output = output
        self._ok = ok

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None):
        self.calls.append(
            {"cwd": cwd, "argv": list(argv), "timeout": timeout, "sandbox": sandbox, "label": label}
        )
        return RunOutput(
            command=label or (argv[0] if argv else ""),
            exit_code=0 if self._ok else 1,
            output=self._output,
            timed_out=False,
        )


# ---------------------------------------------------------------------------
# 驗收 #2：_gate_tests 以 argv 走 run_command_exec
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_tests_uses_exec_with_correct_argv(monkeypatch, tmp_path):
    """_gate_tests 必須以固定 argv 走 exec、sandbox=True、timeout=600，且不走 shell。"""
    spy = ExecSpy(output="1 passed in 0.01s", ok=True)
    monkeypatch.setattr(runner, "run_command_exec", spy)

    # shell 版 run_command 若被呼叫即視為遷移失敗。
    async def _boom_rc(*a, **k):
        raise AssertionError("_gate_tests 不應呼叫 shell 版 run_command")

    monkeypatch.setattr(runner, "run_command", _boom_rc)

    ok, out = await autopilot._gate_tests(str(tmp_path))

    assert ok is True
    assert "passed" in out
    assert len(spy.calls) == 1
    c = spy.calls[0]
    # 用 sys.executable（當前直譯器）而非裸 "python"，避免 PATH 無 python 的解析失敗。
    assert c["argv"][0] == sys.executable, c["argv"]
    assert c["argv"][1:] == ["-m", "pytest", "-q"], c["argv"]
    assert c["sandbox"] is True, "pytest 閘門須顯式 sandbox=True（不可依賴預設）"
    assert c["timeout"] == 600
    assert c["label"] == "pytest gate"


@pytest.mark.asyncio
async def test_gate_tests_failure_propagates(monkeypatch, tmp_path):
    """exec 回傳非零（pytest 失敗）時，_gate_tests 回 False 並帶尾段輸出。"""
    spy = ExecSpy(output="x" * 3000 + "1 failed", ok=False)
    monkeypatch.setattr(runner, "run_command_exec", spy)

    ok, out = await autopilot._gate_tests(str(tmp_path))
    assert ok is False
    assert len(out) <= 1500, "輸出應截尾為最後 1500 字"
    assert out.endswith("1 failed")


# ---------------------------------------------------------------------------
# 驗收 #2：原始碼層級——_gate_tests 不再以 shell run_command 跑 pytest
# ---------------------------------------------------------------------------
def test_source_gate_tests_uses_exec_not_shell():
    src = inspect.getsource(autopilot._gate_tests)
    assert "run_command_exec(" in src, "_gate_tests 應改用 run_command_exec"
    shell_hits = re.findall(r"(?<!_exec)\brun_command\(", src)
    assert not shell_hits, f"_gate_tests 仍殘留 shell run_command：{len(shell_hits)} 處"
    # argv 用 sys.executable（避免 PATH 問題）+ 固定 pytest 參數，不再是 shell 字串。
    assert "sys.executable" in src
    assert '"-m", "pytest", "-q"' in src


# ---------------------------------------------------------------------------
# 驗收 #5：遷移後共用的 exec 路徑——含 metacharacter 的參數一律當純文字
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_migrated_exec_treats_metachars_as_literal(tmp_path):
    """端到端：經 run_command_exec（autopilot/runner 遷移後共用的執行路徑）傳入含
    ;/&&/$()/`` 的參數，必須原樣當純文字，且絕不觸發任何被注入的子指令。
    """
    payload = "; touch semi && touch andand $(touch dollar) `touch backtick`"
    r = await runner.run_command_exec(
        tmp_path,
        [
            "python3",
            "-c",
            "import sys,pathlib;pathlib.Path('out.txt').write_text(sys.argv[1])",
            payload,
        ],
        sandbox=False,
    )
    assert r.ok, r.output
    # 沒有任何被注入的指令真的執行（哨兵檔不存在）。
    for ghost in ("semi", "andand", "dollar", "backtick"):
        assert not (tmp_path / ghost).exists(), f"注入指令被執行：{ghost}"
    # payload 原樣（含 metacharacter）傳給程式。
    assert (tmp_path / "out.txt").read_text() == payload
