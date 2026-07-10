"""寫時 lint（studio/lint.py + experts PostToolUse hook + tools parity,效率強化 A）。

治「lint 事後才紅」（#249/#496/#364/#367 三輪各燒 1-2 小時只為空格）：.py 寫入的當下
自動 ruff safe 修復＋排版，殘餘違規回饋專家當場修。

守護不變量：
- lint_file 六態：殘餘違規回文字/自動改寫全綠回「重新 Read」提醒/全綠無改寫回 None/
  非 .py 回 None/無 ruff 回 None/子程序爆炸與逾時回 None（fail-open）。
- resolve_ruff 優先序：cwd/.venv/bin/ruff > sys.executable -m ruff > None。
- hook 接線：旋鈕開 → PostToolUse matcher="Write|Edit|MultiEdit";旋鈕關 → 無 PostToolUse
  且 PreToolUse FS guard 不受波及。
- tools parity：write_file/edit_file 附 [lint] 段且不觸發 _is_error_result。
全程 mock subprocess/lint_file，零真實 ruff 子程序以外的系統動作。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from studio import config, experts, lint


def _install_fake_sdk(monkeypatch):
    """CI 無 claude_agent_sdk:_expert_hooks 會在呼叫時 import HookMatcher,裝假模組
    (範式同 tests/test_experts.py;HookMatcher 需吃 timeout kwarg——真 SDK 有此欄位)。"""
    mod = types.ModuleType("claude_agent_sdk")

    class HookMatcher:
        def __init__(self, matcher=None, hooks=None, timeout=None):
            self.matcher = matcher
            self.hooks = hooks or []
            self.timeout = timeout

    mod.HookMatcher = HookMatcher
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(lint, "_ruff_cache", {})
    monkeypatch.setattr(config, "EXPERT_LINT_HOOK", True)
    monkeypatch.setattr(config, "EXPERT_LINT_TIMEOUT", 5.0)


class FakeRuns:
    """monkeypatch lint._run：依呼叫序回傳 (rc, out)，並記錄命令。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd):
        self.calls.append(list(cmd))
        if isinstance(self.results[0], Exception):
            raise self.results.pop(0)
        return self.results.pop(0)


def _py(tmp_path, content="x = 1\n"):
    f = tmp_path / "mod.py"
    f.write_text(content, encoding="utf-8")
    return f


# --- lint_file 六態 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_residual_violations_return_feedback(tmp_path, monkeypatch):
    f = _py(tmp_path)
    monkeypatch.setattr(lint, "resolve_ruff", lambda cwd: ["ruff"])
    runs = FakeRuns([(0, ""), (0, ""), (1, "mod.py:1:1: E402 module level import not at top")])
    monkeypatch.setattr(lint, "_run", runs)

    out = await lint.lint_file(tmp_path, str(f))

    assert out and "[lint]" in out and "E402" in out and "當場修正" in out
    assert runs.calls[0][:3] == ["ruff", "check", "--fix"]
    assert runs.calls[1][:2] == ["ruff", "format"]


@pytest.mark.asyncio
async def test_autofixed_clean_reminds_reread(tmp_path, monkeypatch):
    f = _py(tmp_path, "x=1\n")
    monkeypatch.setattr(lint, "resolve_ruff", lambda cwd: ["ruff"])

    async def _run(cmd, cwd):
        if "format" in cmd:
            f.write_text("x = 1\n", encoding="utf-8")  # 模擬 ruff 改寫檔案
        return (0, "")

    monkeypatch.setattr(lint, "_run", _run)
    out = await lint.lint_file(tmp_path, str(f))

    assert out and "重新 Read" in out, "自動改寫後必須提醒重讀(防 Edit old_string 過期)"


@pytest.mark.asyncio
async def test_clean_unchanged_returns_none(tmp_path, monkeypatch):
    f = _py(tmp_path)
    monkeypatch.setattr(lint, "resolve_ruff", lambda cwd: ["ruff"])
    monkeypatch.setattr(lint, "_run", FakeRuns([(0, ""), (0, ""), (0, "")]))
    assert await lint.lint_file(tmp_path, str(f)) is None


@pytest.mark.asyncio
async def test_non_py_and_missing_and_no_ruff_silent(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("hi")
    assert await lint.lint_file(tmp_path, str(tmp_path / "a.md")) is None, "非 .py 靜默"
    assert await lint.lint_file(tmp_path, str(tmp_path / "ghost.py")) is None, "檔不存在靜默"
    f = _py(tmp_path)
    monkeypatch.setattr(lint, "resolve_ruff", lambda cwd: None)
    assert await lint.lint_file(tmp_path, str(f)) is None, "無 ruff 靜默"


@pytest.mark.asyncio
@pytest.mark.parametrize("boom", [RuntimeError("subprocess exploded"), asyncio.TimeoutError()])
async def test_failures_are_fail_open(tmp_path, monkeypatch, boom):
    f = _py(tmp_path)
    monkeypatch.setattr(lint, "resolve_ruff", lambda cwd: ["ruff"])
    monkeypatch.setattr(lint, "_run", FakeRuns([boom]))
    assert await lint.lint_file(tmp_path, str(f)) is None, "任何失敗都不得擋寫檔(fail-open)"


# --- resolve_ruff 優先序 --------------------------------------------------------


def test_resolve_ruff_prefers_project_venv(tmp_path):
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "ruff").write_text("#!/bin/sh\n")
    assert lint.resolve_ruff(tmp_path) == [str(bin_dir / "ruff")]


def test_resolve_ruff_falls_back_to_module_or_none(tmp_path, monkeypatch):
    import importlib.util
    import sys

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert lint.resolve_ruff(tmp_path) == [sys.executable, "-m", "ruff"]
    lint._ruff_cache.clear()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert lint.resolve_ruff(tmp_path) is None


# --- experts hook 接線與行為 -----------------------------------------------------


def test_expert_hooks_wiring_knob_on_and_off(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch)
    hooks = experts._expert_hooks(tmp_path)
    assert "PreToolUse" in hooks, "FS guard 恆在"
    assert hooks["PostToolUse"][0].matcher == "Write|Edit|MultiEdit"

    monkeypatch.setattr(config, "EXPERT_LINT_HOOK", False)
    hooks_off = experts._expert_hooks(tmp_path)
    assert "PostToolUse" not in hooks_off, "旋鈕關閉不得掛 lint hook"
    assert "PreToolUse" in hooks_off, "FS guard 不受旋鈕波及"


@pytest.mark.asyncio
async def test_lint_hook_feedback_and_silence(tmp_path, monkeypatch):
    hook = experts._make_lint_hook(tmp_path)

    async def _fake_lint(cwd, fp):
        return "[lint] mod.py:1:1: F401 unused import"

    monkeypatch.setattr(experts.lint, "lint_file", _fake_lint)
    out = await hook({"tool_name": "Write", "tool_input": {"file_path": "mod.py"}}, "id1", None)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "F401" in out["hookSpecificOutput"]["additionalContext"]

    async def _none(cwd, fp):
        return None

    monkeypatch.setattr(experts.lint, "lint_file", _none)
    assert await hook({"tool_name": "Write", "tool_input": {"file_path": "a.py"}}, "i", None) == {}
    # 非目標工具/缺 file_path/例外 → 一律 {}
    assert await hook({"tool_name": "Bash", "tool_input": {}}, "i", None) == {}
    assert await hook({"tool_name": "Edit", "tool_input": {}}, "i", None) == {}

    async def _boom(cwd, fp):
        raise RuntimeError("boom")

    monkeypatch.setattr(experts.lint, "lint_file", _boom)
    assert await hook({"tool_name": "Write", "tool_input": {"file_path": "a.py"}}, "i", None) == {}


# --- tools.execute parity --------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_write_and_edit_append_lint_note(tmp_path, monkeypatch):
    from studio import tools

    async def _fake_lint(cwd, fp):
        return "[lint] mod.py:1:1: E402 ..."

    monkeypatch.setattr(tools.lint, "lint_file", _fake_lint)
    out = await tools.execute("write_file", {"path": "mod.py", "content": "x=1\n"}, tmp_path)
    assert out.startswith("已寫入") and "[lint]" in out
    assert not tools._is_error_result(out), "lint 附加段不得被誤判為副作用失敗"

    out2 = await tools.execute(
        "edit_file", {"path": "mod.py", "old": "x=1", "new": "x=2"}, tmp_path
    )
    assert out2.startswith("已修改") and "[lint]" in out2


@pytest.mark.asyncio
async def test_tools_silent_when_lint_none(tmp_path, monkeypatch):
    from studio import tools

    async def _none(cwd, fp):
        return None

    monkeypatch.setattr(tools.lint, "lint_file", _none)
    out = await tools.execute("write_file", {"path": "b.py", "content": "y=1\n"}, tmp_path)
    assert out == "已寫入 b.py"


# --- 真 ruff 冒煙(本機 studio venv 有 pinned ruff;無網路、單檔、快) -----------------


@pytest.mark.asyncio
async def test_real_ruff_smoke_fixes_and_reports(tmp_path):
    if lint.resolve_ruff(tmp_path) is None:
        pytest.skip("此環境無 ruff")
    f = tmp_path / "smoke.py"
    f.write_text("import os\nimport sys\nx=1\n", encoding="utf-8")  # F401×2 + 格式
    out = await lint.lint_file(tmp_path, str(f))
    # safe fix 會移除未用 import 或回報之;至少要有回饋或已改寫
    assert out is not None, "有未用 import/格式問題,不應完全靜默"
