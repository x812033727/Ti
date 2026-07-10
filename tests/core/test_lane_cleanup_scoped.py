"""lane 收尾清理 session-scoped(效能/穩定強化 B2)。

背景(2026-07-10 journal 實證):`Orchestrator.run()` 的 finally 原本 `shutil.rmtree`
**整個共享 lanes root**——被放棄的舊 session(_cancel_and_reclaim 60s 放棄後 finally 仍在
慢跑)會把「下一個任務」正在用的活 lane 一併端走,新 lane 的 SDK 子行程以已消失的 cwd
spawn 直接 `FileNotFoundError: .../lane-apbbe769ec75-3`、整波 CLIConnectionError 重試。

守護不變量:
- finally 只刪 `lane-{自己 session_id}-*` 前綴目錄;**他場的 lane 目錄必須保留**。
- lanes root 只在已空時移除(非空=有他場活 lane → 保留)。
- 單一 session 正常收尾後(只有自己的 lane)root 消失——與舊行為等價。
"""

from __future__ import annotations

import pytest

from studio import events
from studio.orchestrator import StudioSession


def _collect():
    bucket: list = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


async def _run_session_to_finally(tmp_path, sid: str):
    """跑一場最小 session(experts 注入空 dict,offline 快速走完)讓 finally 執行。"""
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    _, bc = _collect()
    session = StudioSession(sid, bc, experts={}, cwd=cwd)

    async def _noop_run(requirement):
        return {}

    # 只驗 finally 的清理語意:把主流程 _run 換成 no-op,run() 的 try/finally 仍照走。
    session._run = _noop_run
    await session.run("需求")
    return cwd


@pytest.mark.asyncio
async def test_finally_keeps_other_sessions_lanes(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    lanes_root = tmp_path / "work.lanes"
    mine = lanes_root / "lane-sidA-1"
    theirs = lanes_root / "lane-sidB-7"
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)
    (theirs / "alive.txt").write_text("他場活 lane")

    await _run_session_to_finally(tmp_path, "sidA")

    assert not mine.exists(), "自己的殘留 lane 應被清掉"
    assert theirs.exists(), "他場活 lane 絕不可被端走(FileNotFoundError 整波重試的根因)"
    assert lanes_root.exists(), "root 非空(有他場 lane)不得移除"


@pytest.mark.asyncio
async def test_finally_removes_root_when_only_own_lanes(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    lanes_root = tmp_path / "work.lanes"
    (lanes_root / "lane-sidA-1").mkdir(parents=True)
    (lanes_root / "lane-sidA-2-3").mkdir(parents=True)

    await _run_session_to_finally(tmp_path, "sidA")

    assert not lanes_root.exists(), "只剩自己的 lane 時,清完應連 root 一併移除(與舊行為等價)"


@pytest.mark.asyncio
async def test_finally_noop_when_no_lanes_root(tmp_path):
    cwd = await _run_session_to_finally(tmp_path, "sidA")
    assert cwd.exists()
    assert not (tmp_path / "work.lanes").exists()
