"""QA 獨立驗證（任務 #4）：tools._safe_path 委派 safe_resolve，行為與原先一致。

重點：_safe_path 是 workspace.safe_resolve 的薄包裝（單一 containment 真實來源），
讀/編輯 must_exist=True、寫 must_exist=False；target==root 放行；無循環 import。
本檔走 execute() 端到端，釘死各工具的安全邊界。
"""

from __future__ import annotations

import pytest

from studio import tools

# --- 標準 5：_safe_path 委派 + 無循環 import ---


def test_safe_path_delegates_to_workspace():
    import inspect

    from studio import workspace

    src = inspect.getsource(tools._safe_path)
    assert "safe_resolve" in src
    # tools 不再自寫 containment 比對
    assert "not in target.parents" not in src
    assert tools.safe_resolve is workspace.safe_resolve


def test_no_circular_import():
    import importlib

    import studio.tools as t
    import studio.workspace as w

    importlib.reload(w)
    importlib.reload(t)
    assert t.safe_resolve is w.safe_resolve


def test_safe_path_target_equals_root_allowed(tmp_path):
    # target == root 放行（與原行為一致）
    assert tools._safe_path(tmp_path, "") == tmp_path.resolve()


def test_safe_path_internal_symlink_allowed(tmp_path):
    (tmp_path / "real.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    assert tools._safe_path(tmp_path, "link.txt") == (tmp_path / "real.txt").resolve()


def test_safe_path_external_symlink_blocked(tmp_path):
    outside = tmp_path.parent / "out.txt"
    outside.write_text("x", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "leak").symlink_to(outside)
    assert tools._safe_path(ws, "leak") is None


# --- 標準 6：execute() 端到端 5 類邊界 ---


@pytest.mark.asyncio
async def test_read_external_symlink_blocked(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("LEAK", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "leak").symlink_to(secret)
    out = await tools.execute("read_file", {"path": "leak"}, ws)
    assert "找不到" in out
    assert "LEAK" not in out


@pytest.mark.asyncio
async def test_read_internal_symlink_allowed(tmp_path):
    (tmp_path / "real.txt").write_text("OK", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "real.txt")
    out = await tools.execute("read_file", {"path": "alias.txt"}, tmp_path)
    assert out == "OK"


@pytest.mark.asyncio
async def test_read_dotdot_blocked(tmp_path):
    (tmp_path.parent / "p.txt").write_text("X", encoding="utf-8")
    out = await tools.execute("read_file", {"path": "../p.txt"}, tmp_path)
    assert "找不到" in out


@pytest.mark.asyncio
async def test_write_dotdot_and_absolute_blocked(tmp_path):
    assert "超出" in await tools.execute(
        "write_file", {"path": "../evil.txt", "content": "x"}, tmp_path
    )
    assert "超出" in await tools.execute(
        "write_file", {"path": "/etc/evil.txt", "content": "x"}, tmp_path
    )
    assert not (tmp_path.parent / "evil.txt").exists()


@pytest.mark.asyncio
async def test_write_new_file_allowed_must_exist_false(tmp_path):
    # 寫新檔（尚未存在）必須放行，否則回歸
    out = await tools.execute("write_file", {"path": "sub/new.txt", "content": "hi"}, tmp_path)
    assert "已寫入" in out
    assert (tmp_path / "sub" / "new.txt").read_text() == "hi"


@pytest.mark.asyncio
async def test_write_through_existing_external_symlink_dir_blocked(tmp_path):
    # 前綴是『已存在』的外部目錄 symlink，往其中寫新檔 → resolve 展開前綴 → 擋下
    outside = tmp_path.parent / "outdir"
    outside.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "linkdir").symlink_to(outside)
    out = await tools.execute("write_file", {"path": "linkdir/evil.txt", "content": "x"}, ws)
    assert "超出" in out
    assert not (outside / "evil.txt").exists()


@pytest.mark.asyncio
async def test_edit_missing_rejected(tmp_path):
    out = await tools.execute("edit_file", {"path": "nope.txt", "old": "a", "new": "b"}, tmp_path)
    assert "找不到" in out


@pytest.mark.asyncio
async def test_edit_external_symlink_blocked(tmp_path):
    secret = tmp_path.parent / "s.txt"
    secret.write_text("aaa", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "leak").symlink_to(secret)
    out = await tools.execute("edit_file", {"path": "leak", "old": "a", "new": "b"}, ws)
    assert "找不到" in out
    assert secret.read_text() == "aaa"  # 未被改動
