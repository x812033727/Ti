"""並行波次排程 e2e（真 git worktree + 注入 lane 專家工廠）。

驗證 PARALLEL_TASKS_ENABLED 開啟時：
- 一波內的獨立任務切成多條 lane 並行，每條各自 worktree 分支 + 獨立專家團隊（工廠按 lane
  被呼叫、實例不共用）。
- 各 lane 在自己的 worktree 寫檔/commit，波末序列化合併回主分支（主 repo 最終含所有 lane 成果）。
- NOTES 在波末序列化 flush，含每個任務摘要。
- 並行 lane 的 expert_message 事件帶 task_id 供前端分流。
- 看板最終全部 done。
"""

from __future__ import annotations

import pytest

from studio import config, events, workspace
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role


class MainStub:
    """主（循序）專家：PM 給拆解/驗收/檢討腳本；其餘角色在並行模式的任務階段不會被呼叫。"""

    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = list(scripts)
        self.calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        text = self._scripts[min(self.calls, len(self._scripts) - 1)] if self._scripts else "ok"
        self.calls += 1
        return text

    async def stop(self) -> None:
        pass


class LaneStub:
    """並行 lane 專家：工程師在自己的 worktree 寫一個「以 worktree 命名」的唯一檔（避免合併衝突）。

    每次發言都廣播一則 expert_message（經 _tagged_broadcast 後應帶 task_id）。
    """

    created: list[tuple[str, str]] = []  # (session_suffix, role_key)

    def __init__(self, role: Role, session_id: str, cwd):
        self.role = role
        self.session_id = session_id
        self.cwd = cwd
        LaneStub.created.append((session_id, role.key))

    async def speak(self, prompt: str, broadcast) -> str:
        await broadcast(
            events.expert_message(
                self.session_id, self.role.key, self.role.name, self.role.avatar, "做事中"
            )
        )
        if self.role.key == "engineer":
            (self.cwd / f"{self.cwd.name}.txt").write_text("done\n", encoding="utf-8")
            return "已完成實作"
        if self.role.key == "qa":
            return "驗證: PASS"
        if self.role.key == "senior":
            return "決議: 核可"
        return "ok"

    async def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_parallel_wave_isolates_and_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", 3)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    LaneStub.created.clear()

    sid = "par"
    cwd = workspace.create_workspace(sid)

    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    # 主專家：PM 拆 3 個彼此獨立的任務（無依賴 → 單一波次 3 條 lane）。
    main = {
        "pm": MainStub(
            BY_KEY["pm"], ["任務: #1 甲\n任務: #2 乙\n任務: #3 丙", "決議: 完成", "檢討"]
        ),
        "engineer": MainStub(BY_KEY["engineer"], ["x"]),
        "qa": MainStub(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": MainStub(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(sid, broadcast, experts=main, cwd=cwd)
    session._lane_expert_factory = LaneStub  # 注入 lane 專家工廠

    result = await session.run("做三件獨立的事")

    # 1) 三條 lane 各建獨立專家團隊（工廠按 lane 被呼叫，session 後綴互異）。
    suffixes = {s for s, _ in LaneStub.created}
    assert len(suffixes) == 3, f"應有 3 條獨立 lane 的專家團隊：{suffixes}"

    # 2) 看板最終全部 done。
    assert all(t["status"] == "done" for t in session._tasks)
    assert result["completed"] is True

    # 3) 各 lane 的 worktree 成果都序列化合併回主分支。
    files = set(workspace.list_files(sid))
    assert {"task-1.txt", "task-2.txt", "task-3.txt"} <= files, f"主分支缺 lane 成果：{files}"

    # 4) NOTES 含每個任務摘要（波末序列化 flush）。
    notes = workspace.read_notes(sid)
    assert "任務 #1 完成" in notes and "任務 #2 完成" in notes and "任務 #3 完成" in notes

    # 5) 並行 lane 的 expert_message 帶 task_id。
    lane_msgs = [
        e for e in bucket if e.type == events.EventType.EXPERT_MESSAGE and "task_id" in e.payload
    ]
    assert lane_msgs, "並行 lane 的 expert_message 應帶 task_id"
    assert {e.payload["task_id"] for e in lane_msgs} == {1, 2, 3}


@pytest.mark.asyncio
async def test_parallel_disabled_is_sequential(tmp_path, monkeypatch):
    """關閉並行旗標時不開任何 worktree、不呼叫 lane 工廠（純循序、行為不變）。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    LaneStub.created.clear()

    sid = "seq"
    cwd = workspace.create_workspace(sid)

    async def broadcast(ev):
        pass

    main = {
        "pm": MainStub(BY_KEY["pm"], ["任務: #1 甲\n任務: #2 乙", "決議: 完成", "檢討"]),
        "engineer": MainStub(BY_KEY["engineer"], ["實作好了"]),
        "qa": MainStub(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": MainStub(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(sid, broadcast, experts=main, cwd=cwd)
    session._lane_expert_factory = LaneStub

    await session.run("做兩件事")

    assert LaneStub.created == [], "關閉並行時不應建立任何 lane 專家"
    assert not (tmp_path / f"{cwd.name}.lanes").exists(), "關閉並行時不應建立 worktree 目錄"
    assert all(t["status"] == "done" for t in session._tasks)
