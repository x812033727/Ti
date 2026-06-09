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
