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

from studio import config, events, runner, workspace
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


class RaisingLaneStub:
    """並行 lane 專家：工程師發言時直接拋例外，模擬 lane 中途崩潰（驗證 worktree 不洩漏）。"""

    def __init__(self, role: Role, session_id: str, cwd):
        self.role = role
        self.session_id = session_id
        self.cwd = cwd

    async def speak(self, prompt: str, broadcast) -> str:
        if self.role.key == "engineer":
            raise RuntimeError("模擬 lane 崩潰")
        return "ok"

    async def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_lane_exception_cleans_up_worktrees(tmp_path, monkeypatch):
    """lane 中途拋例外（未走到 teardown）時，run() 收尾仍清掉 .lanes worktree，不洩漏。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", 3)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    sid = "boom"
    cwd = workspace.create_workspace(sid)

    async def broadcast(ev):
        pass

    main = {
        "pm": MainStub(BY_KEY["pm"], ["任務: #1 甲\n任務: #2 乙", "決議: 未完成", "檢討"]),
        "engineer": MainStub(BY_KEY["engineer"], ["x"]),
        "qa": MainStub(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": MainStub(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(sid, broadcast, experts=main, cwd=cwd)
    session._lane_expert_factory = RaisingLaneStub

    # run() 吞掉 lane 例外（broadcast error）並跑到收尾，不應往外拋。
    await session.run("會崩潰的兩件事")

    # 收尾後 .lanes worktree 目錄與 git worktree 註冊都清乾淨，無殘留。
    assert not (tmp_path / f"{cwd.name}.lanes").exists(), "lane 例外後 worktree 目錄洩漏"
    wt = await runner.run_command_exec(cwd, ["git", "worktree", "list"], sandbox=False)
    assert "task-1" not in wt.output and "task-2" not in wt.output, wt.output


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
async def test_lane_exception_retries_on_main(tmp_path, monkeypatch):
    """lane 崩潰時，其任務改在主幹序列化重跑（與合併衝突 fallback 對稱）→ 最終 done、指標有記。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", 3)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    sid = "retry"
    cwd = workspace.create_workspace(sid)

    async def broadcast(ev):
        pass

    # lane 一律崩潰；任務轉主幹重跑時改用這套主專家（engineer 正常回應 → 重跑通過）。
    main = {
        "pm": MainStub(BY_KEY["pm"], ["任務: #1 甲", "決議: 完成", "檢討"]),
        "engineer": MainStub(BY_KEY["engineer"], ["主幹重跑實作"]),
        "qa": MainStub(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": MainStub(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(sid, broadcast, experts=main, cwd=cwd)
    session._lane_expert_factory = RaisingLaneStub

    result = await session.run("一件會在 lane 崩潰的事")

    # 任務在主幹序列化重跑 → 最終 done，不靜默卡在 doing/review。
    assert all(t["status"] == "done" for t in session._tasks)
    assert result["completed"] is True
    # 降級指標記到 1 次 lane 例外。
    assert session._parallel_metrics["lane_exceptions"] == 1
    # 崩潰 lane 的 worktree 清乾淨、無洩漏。
    assert not (tmp_path / f"{cwd.name}.lanes").exists()


@pytest.mark.asyncio
async def test_parallel_lane_events_carry_task_id(tmp_path, monkeypatch):
    """並行 lane 的看板/驗證/commit 事件都帶 task_id，前端才能把交錯事件歸到正確任務。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", 3)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    LaneStub.created.clear()

    sid = "tagged"
    cwd = workspace.create_workspace(sid)

    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    main = {
        "pm": MainStub(
            BY_KEY["pm"], ["任務: #1 甲\n任務: #2 乙\n任務: #3 丙", "決議: 完成", "檢討"]
        ),
        "engineer": MainStub(BY_KEY["engineer"], ["x"]),
        "qa": MainStub(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": MainStub(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(sid, broadcast, experts=main, cwd=cwd)
    session._lane_expert_factory = LaneStub

    await session.run("做三件獨立的事")

    # task_status 只由 lane 任務路徑發出 → 並行時每筆都應帶 task_id，且涵蓋三個任務。
    status_evs = [e for e in bucket if e.type == events.EventType.TASK_STATUS]
    assert status_evs, "應有 task_status 事件"
    assert all("task_id" in e.payload for e in status_evs), "並行 task_status 未帶 task_id"
    assert {e.payload["task_id"] for e in status_evs} == {1, 2, 3}

    # 任務 commit（_work_task 內）也帶 task_id（PM 規劃/合併等主幹 commit 不在此列）。
    commit_tagged = {
        e.payload["task_id"]
        for e in bucket
        if e.type == events.EventType.GIT_COMMIT and "task_id" in e.payload
    }
    assert commit_tagged == {1, 2, 3}, f"任務 commit 未正確帶 task_id：{commit_tagged}"

    # 驗證結果事件（run_result）在並行 lane 也帶 task_id。
    run_evs = [e for e in bucket if e.type == events.EventType.RUN_RESULT]
    assert run_evs and all("task_id" in e.payload for e in run_evs), "並行 run_result 未帶 task_id"


class DepStub(LaneStub):
    """記錄各 lane 開工時，其 worktree 是否已看得到前一波合併進來的成果。"""

    saw_prior: dict[str, bool] = {}

    async def speak(self, prompt: str, broadcast) -> str:
        if self.role.key == "engineer":
            DepStub.saw_prior[self.cwd.name] = (self.cwd / "task-1.txt").is_file()
        return await super().speak(prompt, broadcast)


@pytest.mark.asyncio
async def test_dependent_tasks_run_in_later_wave_on_merged_base(tmp_path, monkeypatch):
    """依賴任務排在後一波，其 worktree 從『前一波已合併的 HEAD』分支 → 看得到前一波成果。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    LaneStub.created.clear()
    DepStub.saw_prior = {}

    sid = "dep"
    cwd = workspace.create_workspace(sid)

    async def broadcast(ev):
        pass

    main = {
        "pm": MainStub(
            BY_KEY["pm"], ["任務: #1 基礎\n任務: #2 依賴\n依賴: #2 -> #1", "決議: 完成", "檢討"]
        ),
        "engineer": MainStub(BY_KEY["engineer"], ["x"]),
        "qa": MainStub(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": MainStub(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(sid, broadcast, experts=main, cwd=cwd)
    session._lane_expert_factory = DepStub

    await session.run("先做基礎再做依賴")

    # task-1（第一波）開工時看不到自己；task-2（第二波）開工時應已看得到 task-1.txt。
    assert DepStub.saw_prior.get("task-1") is False
    assert DepStub.saw_prior.get("task-2") is True, "第二波 worktree 未從前一波合併後的 HEAD 分支"
    assert {"task-1.txt", "task-2.txt"} <= set(workspace.list_files(sid))


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
