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


# ===== 任務 #1：工具冪等性分類 QA 驗證 =====
# 驗收標準 #1：tools 層有明確分類，至少涵蓋 write_file/edit_file/run_bash，
#             且 read_file/web_fetch 不被納管。
# 驗收標準 #6：以反向黑樣本將「已知限制」測試化（write_file 刻意冪等、run_bash 無法靜態判定）。


def test_classification_exists_as_frozenset():
    # 分類須是明確的集合（單一真實來源），且不可變
    assert isinstance(tools.NON_IDEMPOTENT_TOOLS, frozenset)
    assert hasattr(tools, "is_idempotent")


def test_edit_file_and_run_bash_are_non_idempotent():
    # 標準 #1：edit_file / run_bash 明確納入非冪等集合
    assert "edit_file" in tools.NON_IDEMPOTENT_TOOLS
    assert "run_bash" in tools.NON_IDEMPOTENT_TOOLS
    assert tools.is_idempotent("edit_file") is False
    assert tools.is_idempotent("run_bash") is False


def test_read_file_and_web_fetch_not_managed():
    # 標準 #1：唯讀工具不被納管（不走去重路徑）
    assert "read_file" not in tools.NON_IDEMPOTENT_TOOLS
    assert "web_fetch" not in tools.NON_IDEMPOTENT_TOOLS
    assert tools.is_idempotent("read_file") is True
    assert tools.is_idempotent("web_fetch") is True


def test_classification_covers_all_three_write_tools():
    # 標準 #1：分類須明列 write_file/edit_file/run_bash 三者各自的策略，
    # 每一個都要有可判定的決策（不能漏掉任何一個）。
    decisions = {
        name: tools.is_idempotent(name) for name in ("write_file", "edit_file", "run_bash")
    }
    assert decisions == {"write_file": True, "edit_file": False, "run_bash": False}


# --- 黑樣本 #6a：write_file 刻意歸冪等的「已知限制」釘死 ---
def test_BLACK_write_file_intentionally_idempotent_known_limitation():
    # 已知限制：write_file 覆寫語意下歸冪等、不入去重。
    # 殘留風險＝LLM 重放若改變 content，去重層攔不住（args 不同）。
    # 此測試把該限制顯式化：write_file 必須「不在」非冪等集合，
    # 若有人未來把 write_file 加進集合（誤以為更安全），此黑樣本會紅，
    # 強迫他重新評估「args hash 不同時去重也無效」的事實。
    assert "write_file" not in tools.NON_IDEMPOTENT_TOOLS
    assert tools.is_idempotent("write_file") is True


# --- 黑樣本 #6b：run_bash 無法靜態判冪等，一律保守歸非冪等 ---
@pytest.mark.parametrize(
    "command",
    ["echo hello", "ls -la", "cat README.md", "git push", "curl -X POST http://x"],
)
def test_BLACK_run_bash_non_idempotent_regardless_of_command(command):
    # 已知限制：run_bash 的命令內容無法靜態判斷冪等性。
    # 即使是看似唯讀的 echo/ls/cat，分類仍一律歸非冪等——
    # 分類依「工具名」而非「命令內容」，杜絕 false negative（漏防）。
    # 若有人未來改成解析命令內容來放行某些 bash，此黑樣本鎖死該行為。
    assert tools.is_idempotent("run_bash") is False
    # 分類不因 args 改變而改變
    assert "run_bash" in tools.NON_IDEMPOTENT_TOOLS


def test_unknown_tool_defaults_to_idempotent():
    # 預設策略：未列入非冪等集合者一律視為冪等（不走去重）。
    # 釘死「白名單式納管」語意——只有明列者才被保護。
    assert tools.is_idempotent("some_future_tool") is True
