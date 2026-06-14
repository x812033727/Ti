"""git worktree 生命週期（並行支線隔離的基礎建設）。

用真實 git（tmp repo）驗證 runner 的 worktree add / commit / merge / abort / remove。
這些操作的 worktree 路徑在 workspace 沙箱外，故一律 sandbox=False；測試顯式關掉沙箱，
避免環境缺 bwrap 時 git_commit 的 add/commit 走 fail-closed。
"""

from __future__ import annotations

import pytest

from studio import config, runner

pytestmark = pytest.mark.skipif(not runner._git_available(), reason="環境無 git")


@pytest.fixture
def main_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    return tmp_path / "main"


async def _seed(repo):
    repo.mkdir(parents=True, exist_ok=True)
    assert await runner.git_init(repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    assert await runner.git_commit(repo, "base commit") is not None


@pytest.mark.asyncio
async def test_worktree_add_commit_merge_happy(tmp_path, main_repo):
    await _seed(main_repo)
    wt = tmp_path / "lanes" / "lane-task-2"
    assert await runner.git_worktree_add(main_repo, wt, "task-2")
    assert wt.is_dir()

    # lane 改一個「不同檔」並 commit，再合併回主 repo。
    (wt / "feature.txt").write_text("feat\n", encoding="utf-8")
    assert await runner.git_commit(wt, "feat in lane") is not None

    res = await runner.git_merge_worktree(main_repo, "task-2")
    assert res.ok and not res.conflict
    assert (main_repo / "feature.txt").is_file()


def test_write_baseline_gitignore_merges_and_preserves(tmp_path):
    # 純檔案寫入(不需 .git):session 開工前呼叫,讓 junk 從不被追蹤。
    gi = tmp_path / ".gitignore"
    gi.write_text("# 專案自有\nmy_custom_dir/\n", encoding="utf-8")
    runner.write_baseline_gitignore(tmp_path)
    body = gi.read_text(encoding="utf-8")
    assert "my_custom_dir/" in body, "既有內容須保留"
    assert ".venv/" in body and "*.db" in body and ".claude/" in body and ".mcp.json" in body
    # 冪等:重複呼叫不重覆灌
    runner.write_baseline_gitignore(tmp_path)
    assert gi.read_text(encoding="utf-8").count(".venv/") == 1


def test_write_baseline_gitignore_creates_when_absent(tmp_path):
    runner.write_baseline_gitignore(tmp_path)
    assert (tmp_path / ".gitignore").exists()
    assert ".venv/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_git_sanitize_workspace_untracks_junk(tmp_path, main_repo):
    """發佈前淨化:已被追蹤的沙箱/環境 junk(.venv/*.db/HOME dotfiles/.claude)應被 untrack,
    且 baseline 樣式併入 .gitignore;真正的專案檔保留。"""
    await _seed(main_repo)
    # 模擬被早期 git add -A 收進去的污染 + 真實專案檔
    (main_repo / "app.py").write_text("print('real')\n", encoding="utf-8")
    (main_repo / "data.db").write_text("x", encoding="utf-8")
    (main_repo / ".bashrc").write_text("", encoding="utf-8")
    (main_repo / ".mcp.json").write_text("", encoding="utf-8")
    (main_repo / ".idea").write_text("", encoding="utf-8")  # 沙箱可能建 0-byte 檔(非目錄)
    (main_repo / ".venv").mkdir()
    (main_repo / ".venv" / "pyvenv.cfg").write_text("home=/x\n", encoding="utf-8")
    (main_repo / ".claude").mkdir()
    (main_repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    await runner.run_command_exec(main_repo, ["git", "add", "-A"], sandbox=False)
    await runner.run_command_exec(main_repo, ["git", "commit", "-m", "polluted"], sandbox=False)

    tracked_before = (
        await runner.run_command_exec(main_repo, ["git", "ls-files"], sandbox=False)
    ).output
    assert ".venv/pyvenv.cfg" in tracked_before and "data.db" in tracked_before

    await runner.git_sanitize_workspace(main_repo)
    await runner.git_commit(main_repo, "sanitized")

    tracked = (await runner.run_command_exec(main_repo, ["git", "ls-files"], sandbox=False)).output
    # junk 全部 untrack
    for junk in (
        ".venv/pyvenv.cfg",
        "data.db",
        ".bashrc",
        ".mcp.json",
        ".claude/settings.json",
        ".idea",
    ):
        assert junk not in tracked, f"{junk} 應已 untrack:\n{tracked}"
    # 真實專案檔保留
    assert "app.py" in tracked and "base.txt" in tracked
    # baseline 樣式併入 .gitignore
    gi = (main_repo / ".gitignore").read_text(encoding="utf-8")
    assert ".venv/" in gi and "*.db" in gi and ".claude/" in gi


@pytest.mark.asyncio
async def test_git_has_changes_detects_dirty_worktree(tmp_path, main_repo):
    """git_has_changes：乾淨工作樹回 False、有未追蹤/未提交變更回 True。

    用於偵測「工程師那輪聲稱寫檔卻零變更」的幻覺寫檔（_work_task 無進展收斂）。
    """
    await _seed(main_repo)
    assert await runner.git_has_changes(main_repo) is False, "剛 commit 完應乾淨"
    (main_repo / "new.txt").write_text("x\n", encoding="utf-8")  # 未追蹤檔
    assert await runner.git_has_changes(main_repo) is True, "有未追蹤檔應為 dirty"


@pytest.mark.asyncio
async def test_merge_blocked_by_untracked_file_is_recoverable(tmp_path, main_repo):
    """主工作樹有未追蹤檔、lane 帶同名檔時，git 連 merge 都不啟動就報錯——

    訊息不含 "CONFLICT"，過去被誤判為未知硬失敗而靜默吞掉並行成果。這裡確認改判為
    blocked（可復原），呼叫端據此走序列化重跑而非丟棄。實測來源：並行 lane 端到端跑時
    出現的「untracked working tree files would be overwritten by merge」。
    """
    await _seed(main_repo)
    wt = tmp_path / "lanes" / "lane-task-9"
    assert await runner.git_worktree_add(main_repo, wt, "task-9")

    # lane 新增一個「主幹尚未追蹤」的檔並 commit。
    (wt / "feature.txt").write_text("from lane\n", encoding="utf-8")
    assert await runner.git_commit(wt, "lane adds feature.txt") is not None
    # 主工作樹有同名「未追蹤」檔（未 commit）——git merge 會拒絕覆寫。
    (main_repo / "feature.txt").write_text("untracked in main\n", encoding="utf-8")

    res = await runner.git_merge_worktree(main_repo, "task-9")
    assert not res.ok
    assert res.blocked, f"未追蹤檔擋下的合併應判為 blocked：{res.output!r}"
    assert not res.conflict, "工作樹受阻不是內容衝突，不應誤判為 conflict"


@pytest.mark.asyncio
async def test_merge_conflict_detected_then_abort_clean(tmp_path, main_repo):
    await _seed(main_repo)
    wt = tmp_path / "lanes" / "lane-task-3"
    assert await runner.git_worktree_add(main_repo, wt, "task-3")

    # 兩邊改「同一檔」造成衝突。
    (wt / "base.txt").write_text("lane change\n", encoding="utf-8")
    assert await runner.git_commit(wt, "lane edits base") is not None
    (main_repo / "base.txt").write_text("main change\n", encoding="utf-8")
    assert await runner.git_commit(main_repo, "main edits base") is not None

    res = await runner.git_merge_worktree(main_repo, "task-3")
    assert not res.ok and res.conflict

    await runner.git_merge_abort(main_repo)
    st = await runner.run_command_exec(main_repo, ["git", "status", "--porcelain"], sandbox=False)
    assert st.output.strip() == "", f"abort 後 working tree 不乾淨：{st.output!r}"


@pytest.mark.asyncio
async def test_worktree_remove_cleans_dir_and_branch(tmp_path, main_repo):
    await _seed(main_repo)
    wt = tmp_path / "lanes" / "lane-task-4"
    assert await runner.git_worktree_add(main_repo, wt, "task-4")
    assert wt.is_dir()

    await runner.git_worktree_remove(main_repo, wt, "task-4")
    assert not wt.exists()
    br = await runner.run_command_exec(
        main_repo, ["git", "branch", "--list", "task-4"], sandbox=False
    )
    assert br.output.strip() == "", f"分支未刪除：{br.output!r}"


@pytest.mark.asyncio
async def test_current_branch_returns_main(main_repo):
    await _seed(main_repo)
    name = await runner.git_current_branch(main_repo)
    assert name in ("main", "master")


@pytest.mark.asyncio
async def test_worktree_add_rejects_option_like_branch(tmp_path, main_repo):
    await _seed(main_repo)
    # 以 '-' 開頭的 branch 名（選項注入）被 _BRANCH_RE 擋下。
    assert not await runner.git_worktree_add(main_repo, tmp_path / "x", "--upload-pack=evil")


@pytest.mark.asyncio
async def test_merge_rejects_bad_branch_name(main_repo):
    await _seed(main_repo)
    res = await runner.git_merge_worktree(main_repo, "-X")
    assert not res.ok and not res.conflict


@pytest.mark.asyncio
async def test_worktree_disabled_when_git_off(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    assert not await runner.git_worktree_add(tmp_path, tmp_path / "wt", "task-1")
    res = await runner.git_merge_worktree(tmp_path, "task-1")
    assert not res.ok and not res.conflict
