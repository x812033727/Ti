"""Demo 指令 usage-error 消毒（runner.sanitize_demo_command）純函式測試。

背景（#248）：PM 給的 demo 指令帶了工具不認得的參數（`pytest … --cache-dir=…` →
exit 4「unrecognized arguments」），數小時綠色成果被 demo_veto 全數丟棄。消毒規則：
只在「像 usage error」時動作，剝掉 stderr 點名的 token（含同名 option 變體）與引用
未設定環境變數的 token；含 shell 控制符或消毒後無變化則回 None（不重試）。
重試「一次」的語意由呼叫端（orchestrator._final_demo）落地，此處驗證消毒冪等性
（消毒後的指令再失敗也剝不出新東西 → None → 不會二次重試）。
"""

from __future__ import annotations

import pytest

from studio.runner import sanitize_demo_command

_PYTEST_ERR = (
    "ERROR: usage: pytest [options] [file_or_dir] [file_or_dir] [...]\n"
    "pytest: error: unrecognized arguments: --cache-dir=/tmp/x\n"
    "  inifile: pyproject.toml\n"
)


def test_strips_named_unrecognized_equals_token():
    """#248 實案：pytest exit 4、stderr 點名 --cache-dir=/tmp/x → 剝掉該 token。"""
    out = sanitize_demo_command("pytest -q --cache-dir=/tmp/x tests/", 4, _PYTEST_ERR)
    assert out == "pytest -q tests/"


def test_strips_option_name_variant():
    """stderr 點名 `--cache-dir`（不含 =value）時，指令內的 `--cache-dir=…` 也要剝。"""
    err = "pytest: error: unrecognized arguments: --cache-dir\n"
    out = sanitize_demo_command("pytest --cache-dir=/somewhere -q tests/", 4, err)
    assert out == "pytest -q tests/"


def test_strips_option_and_separate_value():
    """argparse 把 option 與其空白分隔的值一起點名：兩個 token 都剝。"""
    err = "prog: error: unrecognized arguments: --foo bar\n"
    out = sanitize_demo_command("prog --foo bar positional", 2, err)
    assert out == "prog positional"


def test_click_no_such_option():
    err = "Usage: tool [OPTIONS]\nTry 'tool --help' for help.\n\nError: No such option: --frob\n"
    out = sanitize_demo_command("tool --frob run", 2, err)
    assert out == "tool run"


def test_strips_unset_env_var_token_on_usage_error():
    """usage error 且 token 引用未設定的環境變數（$VAR 展不開）→ 剝掉該壞路徑片段。"""
    err = "ERROR: usage: pytest [options]\npytest: error: file or directory not found\n"
    out = sanitize_demo_command("pytest $TI_NO_SUCH_VAR_XYZ tests/", 4, err)
    assert out == "pytest tests/"


def test_non_usage_failure_returns_none():
    """一般失敗（真的紅：exit 1 + traceback）不是指令寫壞，不得消毒重試。"""
    assert sanitize_demo_command("pytest tests/", 1, "FAILED tests/test_x.py::test_y\n") is None


def test_exit_2_without_argparse_style_returns_none():
    """exit 2 但無 usage:/error:/點名樣式（如 pytest 收集炸掉）不消毒。"""
    assert sanitize_demo_command("pytest tests/", 2, "INTERNALERROR> boom\n") is None


def test_shell_control_chars_bail_out():
    """含 pipe／&&／重導向的指令 token 化重組會破壞語法，一律不動。"""
    err = "pytest: error: unrecognized arguments: --cache-dir=/x\n"
    for cmd in (
        "pytest --cache-dir=/x && echo ok",
        "pytest --cache-dir=/x | tee log",
        "pytest --cache-dir=/x > out.txt",
        "pytest $(cat args) --cache-dir=/x",
    ):
        assert sanitize_demo_command(cmd, 4, err) is None


def test_nothing_to_strip_returns_none():
    """stderr 點名的 token 不在指令內（或沒點名任何 token）→ 無可剝除，不重試。"""
    err = "pytest: error: unrecognized arguments: --not-in-cmd\n"
    assert sanitize_demo_command("pytest -q tests/", 4, err) is None


def test_would_strip_everything_returns_none():
    err = "prog: error: unrecognized arguments: prog\n"
    assert sanitize_demo_command("prog", 2, err) is None


def test_sanitize_is_idempotent_single_retry():
    """消毒後的指令若再以同樣 stderr 失敗，二次消毒剝不出新東西 → None（單次重試語意）。"""
    first = sanitize_demo_command("pytest -q --cache-dir=/tmp/x tests/", 4, _PYTEST_ERR)
    assert first == "pytest -q tests/"
    assert sanitize_demo_command(first, 4, _PYTEST_ERR) is None


@pytest.mark.parametrize("exit_code", [0, 1, 137])
def test_unrecognized_text_triggers_regardless_of_exit_code(exit_code):
    """偵測以 stderr 點名為主：只要有 unrecognized arguments 樣式即嘗試消毒。"""
    err = "prog: error: unrecognized arguments: --bad\n"
    assert sanitize_demo_command("prog --bad go", exit_code, err) == "prog go"
