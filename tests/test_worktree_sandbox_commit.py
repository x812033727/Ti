"""回歸：並行 lane（linked worktree）在沙箱開啟時，git_commit 必須真的能 commit。

歷史 bug（session 0ca966dba5fd，2026-06-10）：_bwrap_prefix 只把 lane cwd 綁為可寫，但
worktree 的 git 寫入（index.lock / refs / objects）落在主 repo 的共用 .git——那條路徑不在
cwd 內，踩到 `--ro-bind / /` 的唯讀面，git add 以「Read-only file system」失敗。git_commit
因而回 None、lane 分支拿不到 commit，波次合併變成「Already up to date」no-op，產出全數遺失、
驗收 0/N。既有 test_worktree.py 全程 SANDBOX_ENABLED=False，故沒蓋到這條路徑。

修法：_bwrap_prefix 偵測 cwd 為 linked worktree 時，額外把共用 .git 綁為可寫。
"""

from __future__ import annotations

import pytest

from studio import config, runner

pytestmark = pytest.mark.skipif(not runner._git_available(), reason="環境無 git")


def test_common_git_dir_resolves_for_linked_worktree(tmp_path):
    """linked worktree 的 .git 是檔案 → 解析出主 repo 的共用 .git；一般 repo → None。"""
    main = tmp_path / "main"
    main.mkdir()
    (main / ".git").mkdir()  # 一般 repo：.git 是目錄
    assert runner._worktree_common_git_dir(main) is None

    lane = tmp_path / "main.lanes" / "task-1"
    lane.mkdir(parents=True)
    admin = main / ".git" / "worktrees" / "task-1"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (lane / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")

    resolved = runner._worktree_common_git_dir(lane)
    assert resolved is not None
    assert resolved == (main / ".git").resolve()


def test_bwrap_prefix_binds_worktree_common_git_writable(tmp_path, monkeypatch):
    """linked worktree 的 _bwrap_prefix 必須含一段把共用 .git 綁為可寫的 --bind。"""
    monkeypatch.setattr(config, "SANDBOX_NET", False)
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "task-2").mkdir(parents=True)
    lane = tmp_path / "main.lanes" / "task-2"
    lane.mkdir(parents=True)
    admin = main / ".git" / "worktrees" / "task-2"
    (lane / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")

    args = runner._bwrap_prefix(lane)
    common = str((main / ".git").resolve())
    # 找出 --bind <common> <common> 連續三元組
    triples = [args[i : i + 3] for i in range(len(args) - 2)]
    assert ["--bind", common, common] in triples, f"缺少共用 .git 的可寫 bind：{args}"


@pytest.mark.asyncio
@pytest.mark.skipif(not config._sandbox_available(), reason="環境無 bwrap，無法驗沙箱路徑")
async def test_sandboxed_lane_commit_and_merge_land_files(tmp_path, monkeypatch):
    """端到端（沙箱開啟）：lane 內 git_commit 成功 → 合併回主 repo 帶進檔案、HEAD 前進。"""
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)

    main = tmp_path / "main"
    main.mkdir()
    assert await runner.git_init(main)
    assert await runner.git_ensure_initial_commit(main) is not None

    wt = tmp_path / "main.lanes" / "task-1"
    assert await runner.git_worktree_add(main, wt, "task-1")
    (wt / "feature.txt").write_text("feat\n", encoding="utf-8")

    # 修前這裡回 None（git add 踩唯讀面失敗）→ 整個鏈路斷掉。
    h = await runner.git_commit(wt, "feat in sandboxed lane")
    assert h is not None, "沙箱內 lane git_commit 應成功 commit"

    before = await runner.git_head_short(main)
    res = await runner.git_merge_worktree(main, "task-1")
    after = await runner.git_head_short(main)
    assert res.ok and not res.conflict
    assert before != after, "合併後主分支 HEAD 應前進（非 Already-up-to-date no-op）"
    assert (main / "feature.txt").is_file(), "lane 產出應落進主 repo 工作樹"
