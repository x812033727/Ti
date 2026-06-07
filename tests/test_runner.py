"""測試確定性執行工具 runner（不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import runner

# --- 解析執行指令 -------------------------------------------------------


def test_parse_run_command():
    assert runner.parse_run_command("總結…\n執行指令: python main.py") == "python main.py"
    assert runner.parse_run_command("執行指令：`python bmi.py`") == "python bmi.py"
    assert runner.parse_run_command("沒有宣告") is None


# --- 偵測入口 -----------------------------------------------------------


def test_detect_entrypoint_prefers_main(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')")
    (tmp_path / "util.py").write_text("x = 1")
    assert runner.detect_entrypoint(tmp_path) == "main.py"


def test_detect_entrypoint_single_py(tmp_path):
    (tmp_path / "bmi.py").write_text("print('hi')")
    assert runner.detect_entrypoint(tmp_path) == "bmi.py"


def test_detect_entrypoint_none_when_ambiguous(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    assert runner.detect_entrypoint(tmp_path) is None


def test_resolve_demo_command(tmp_path):
    assert runner.resolve_demo_command(tmp_path, "python x.py") == "python x.py"
    (tmp_path / "main.py").write_text("")
    assert runner.resolve_demo_command(tmp_path, None) == "python main.py"


# --- 直譯器可攜性（python / python3）-----------------------------------


def test_executable_command_keeps_existing_interpreter(monkeypatch):
    # python 在 PATH 時，指令原封不動。
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert runner._executable_command("python main.py add 3 4") == "python main.py add 3 4"


def test_executable_command_falls_back_to_sys_executable(monkeypatch):
    # python 不在 PATH 時，開頭 token 換成 sys.executable，其餘參數保留。
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(runner.sys, "executable", "/opt/py/python3")
    out = runner._executable_command("python main.py add 3 4")
    assert out == "/opt/py/python3 main.py add 3 4"


def test_executable_command_ignores_non_python(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner._executable_command("echo hi") == "echo hi"


# --- 執行指令 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_ok(tmp_path):
    r = await runner.run_command(tmp_path, "echo hello")
    assert r.ok
    assert "hello" in r.output
    assert r.exit_code == 0


@pytest.mark.asyncio
async def test_run_command_failure(tmp_path):
    r = await runner.run_command(tmp_path, "exit 3")
    assert not r.ok
    assert r.exit_code == 3


@pytest.mark.asyncio
async def test_run_command_timeout(tmp_path):
    r = await runner.run_command(tmp_path, "sleep 5", timeout=1)
    assert r.timed_out
    assert not r.ok


# --- git ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_init_and_commit(tmp_path):
    assert await runner.git_init(tmp_path) is True
    assert (tmp_path / ".git").exists()
    (tmp_path / "f.txt").write_text("hello")
    h = await runner.git_commit(tmp_path, "first commit")
    assert h and len(h) >= 4
    # 無新變更時再 commit 應回 None
    assert await runner.git_commit(tmp_path, "empty") is None
