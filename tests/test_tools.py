"""測試非 Claude provider 的工具層（真實檔案/bash 操作）。"""

from __future__ import annotations

import pytest

from studio import tools


@pytest.mark.asyncio
async def test_write_read_roundtrip(tmp_path):
    assert "已寫入" in await tools.execute(
        "write_file", {"path": "a.txt", "content": "hi"}, tmp_path
    )
    assert await tools.execute("read_file", {"path": "a.txt"}, tmp_path) == "hi"


@pytest.mark.asyncio
async def test_read_missing(tmp_path):
    assert "找不到" in await tools.execute("read_file", {"path": "nope.txt"}, tmp_path)


@pytest.mark.asyncio
async def test_edit_unique_and_ambiguous(tmp_path):
    await tools.execute("write_file", {"path": "f.txt", "content": "a b a"}, tmp_path)
    # 'b' 唯一 → 可改
    assert "已修改" in await tools.execute(
        "edit_file", {"path": "f.txt", "old": "b", "new": "B"}, tmp_path
    )
    assert await tools.execute("read_file", {"path": "f.txt"}, tmp_path) == "a B a"
    # 'a' 出現兩次 → 拒絕
    assert "唯一" in await tools.execute(
        "edit_file", {"path": "f.txt", "old": "a", "new": "X"}, tmp_path
    )


@pytest.mark.asyncio
async def test_run_bash(tmp_path):
    out = await tools.execute("run_bash", {"command": "echo hello"}, tmp_path)
    assert "hello" in out and "exit=0" in out


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_path):
    assert "超出" in await tools.execute(
        "write_file", {"path": "../evil.txt", "content": "x"}, tmp_path
    )


@pytest.mark.asyncio
async def test_absolute_path_blocked(tmp_path):
    assert "超出" in await tools.execute(
        "write_file", {"path": "/etc/evil.txt", "content": "x"}, tmp_path
    )


@pytest.mark.asyncio
async def test_target_equals_root_allowed_for_write(tmp_path):
    # rel="" → target == root（目錄），_safe_path 應放行（與原行為一致），
    # 但寫入目錄會失敗 → 落到工具執行錯誤而非「超出」。
    out = await tools.execute("write_file", {"path": "", "content": "x"}, tmp_path)
    assert "超出" not in out


def test_safe_path_target_equals_root(tmp_path):
    assert tools._safe_path(tmp_path, "") == tmp_path.resolve()


def test_safe_path_inner_symlink_allowed(tmp_path):
    (tmp_path / "real.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    # symlink 指回 workspace 內 → 放行
    assert tools._safe_path(tmp_path, "link.txt") == (tmp_path / "real.txt").resolve()


def test_safe_path_external_symlink_blocked(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "leak").symlink_to(outside)
    assert tools._safe_path(ws, "leak") is None


def test_specs_for_by_role():
    from studio.roles import ENGINEER, PM

    eng = {s["function"]["name"] for s in tools.specs_for(ENGINEER.allowed_tools)}
    assert {"read_file", "write_file", "edit_file", "run_bash"} <= eng
    pm = {s["function"]["name"] for s in tools.specs_for(PM.allowed_tools)}
    assert pm == {"read_file"}


def test_parse_args():
    assert tools.parse_args('{"a": 1}') == {"a": 1}
    assert tools.parse_args("not json") == {}
    assert tools.parse_args({"a": 1}) == {"a": 1}
