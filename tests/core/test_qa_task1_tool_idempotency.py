"""任務 #1：工具冪等性分類的單元測試。

鎖定驗收 #1（分類涵蓋 edit_file/run_bash，read_file/web_fetch 不納管）與
fail-open 預設策略（未知工具視為冪等）。此檔為 NON_IDEMPOTENT_TOOLS 這個
基礎常數提供回歸護欄——任何人改動集合內容都會觸發紅燈，不再是無聲迴歸。
"""

from __future__ import annotations

from studio import tools


def test_non_idempotent_set_exact_membership():
    """集合內容須精確等於 {edit_file, run_bash}，多進或少出都紅。"""
    assert tools.NON_IDEMPOTENT_TOOLS == frozenset({"edit_file", "run_bash"})


def test_edit_file_is_non_idempotent():
    assert tools.is_idempotent("edit_file") is False


def test_run_bash_is_non_idempotent():
    assert tools.is_idempotent("run_bash") is False


def test_write_file_is_idempotent():
    """write_file 覆寫語意 → 天然冪等，刻意不納管。"""
    assert tools.is_idempotent("write_file") is True


def test_read_file_is_idempotent():
    assert tools.is_idempotent("read_file") is True


def test_web_fetch_is_idempotent():
    """web_fetch 唯讀，不被去重路徑納管。"""
    assert tools.is_idempotent("web_fetch") is True


def test_unknown_tool_defaults_to_idempotent_fail_open():
    """黑樣本：未知工具走 fail-open 預設（回 True），驗證白名單式策略。"""
    assert tools.is_idempotent("totally_made_up_tool") is True


def test_frozenset_is_immutable():
    """frozenset 不可變，無 add 方法，防執行期意外竄改。"""
    assert isinstance(tools.NON_IDEMPOTENT_TOOLS, frozenset)
    assert not hasattr(tools.NON_IDEMPOTENT_TOOLS, "add")
