"""離線並行示範 e2e：用真 fake_experts + lane 工廠跑多支線並行（無金鑰、真 git worktree）。

驗證 TI_OFFLINE + TI_PARALLEL_TASKS 一起開時：PM 拆出含依賴的波次任務，第一波兩個獨立模組
分兩條 lane 並行（各自 worktree 寫檔），第二波的整合說明從前一波合併後的 HEAD 分支；最終
主分支含全部檔案、看板全 done、並行 lane 事件帶 task_id。
"""

from __future__ import annotations

import pytest

from studio import config, events, workspace
from studio.fake_experts import build_fake_critics, build_fake_experts, build_fake_lane_expert
from studio.orchestrator import StudioSession


@pytest.mark.asyncio
async def test_offline_parallel_demo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", 3)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    sid = "offpar"
    cwd = workspace.create_workspace(sid)
    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    experts = build_fake_experts(sid, cwd, "做加減法模組與整合說明")
    critics = build_fake_critics(sid, cwd)
    session = StudioSession(sid, broadcast, experts=experts, cwd=cwd, critics=critics)
    session._lane_expert_factory = build_fake_lane_expert

    result = await session.run("做加減法模組與整合說明")

    # 全部任務完成、主分支含各支線 + 依賴任務的成果。
    assert all(t["status"] == "done" for t in session._tasks)
    assert result["completed"] is True
    files = set(workspace.list_files(sid))
    assert {"add.py", "sub.py", "test_add.py", "test_sub.py", "README.md"} <= files, files

    # 並行 lane 的發言帶 task_id（第一波兩條獨立 lane）。
    lane_ids = {
        e.payload["task_id"]
        for e in bucket
        if e.type == events.EventType.EXPERT_MESSAGE and "task_id" in e.payload
    }
    assert {1, 2} <= lane_ids, lane_ids

    # NOTES 整併含各任務摘要。
    notes = workspace.read_notes(sid)
    assert "任務 #1 完成" in notes and "任務 #2 完成" in notes and "任務 #3 完成" in notes

    # 並行可觀測性：done 事件帶並行指標。波次 2、峰值支線 2（第一波 #1/#2 並行）。
    # 註：speedup 在「即時假任務」下可能 <1（worktree/合併的固定開銷大於近乎零的任務工時）——
    # 這是誠實的量測；真實 LLM 任務（每個數秒）才會 >1。故只驗結構與數值合理、不斷言方向。
    done = next(e for e in bucket if e.type == events.EventType.DONE)
    par = done.payload["parallel"]
    assert par["enabled"] is True
    assert par["waves"] == 2 and par["tasks"] == 3
    assert par["lanes_max"] == 2
    assert par["speedup"] > 0 and par["wall_clock_s"] >= 0 and "serial_estimate_s" in par


@pytest.mark.asyncio
async def test_offline_sequential_demo_unchanged(tmp_path, monkeypatch):
    """關閉並行時離線 demo 維持原本四則運算 CLI（行為不變）。"""
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    sid = "offseq"
    cwd = workspace.create_workspace(sid)

    async def broadcast(ev):
        pass

    experts = build_fake_experts(sid, cwd, "四則運算")
    critics = build_fake_critics(sid, cwd)
    session = StudioSession(sid, broadcast, experts=experts, cwd=cwd, critics=critics)
    session._lane_expert_factory = build_fake_lane_expert  # 設了也不該被用到（循序）

    await session.run("四則運算")

    files = set(workspace.list_files(sid))
    assert {"calculator.py", "main.py", "README.md", "test_calculator.py"} <= files
    assert not (tmp_path / f"{cwd.name}.lanes").exists(), "循序不應建立 worktree"
