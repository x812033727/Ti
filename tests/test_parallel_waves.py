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
from studio.orchestrator import LaneContext, LaneResult, StudioSession
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
    assert f"lane-{sid}-1" not in wt.output and f"lane-{sid}-2" not in wt.output, wt.output


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

    # 3) 各 lane 的 worktree 成果都序列化合併回主分支（lane 分支名 = lane-<sid>-<id>）。
    files = set(workspace.list_files(sid))
    expect = {f"lane-{sid}-{i}.txt" for i in (1, 2, 3)}
    assert expect <= files, f"主分支缺 lane 成果：{files}"

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


class ConflictLaneStub(LaneStub):
    """兩條 lane 的工程師都寫同一個 shared.txt（不同內容）→ 合回主幹時 add/add 衝突。

    收到「化解衝突」提示（prompt 含『衝突』）時，就地把 shared.txt 改寫成去除標記的結果，
    模擬工程師在 lane worktree 內解衝突。
    """

    resolved_calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        await broadcast(
            events.expert_message(
                self.session_id, self.role.key, self.role.name, self.role.avatar, "做事中"
            )
        )
        if self.role.key == "engineer":
            shared = self.cwd / "shared.txt"
            if "衝突" in prompt:  # 解衝突回合：清掉標記
                ConflictLaneStub.resolved_calls += 1
                shared.write_text("resolved\n", encoding="utf-8")
                return "已化解衝突"
            shared.write_text(f"{self.cwd.name}\n", encoding="utf-8")  # 各 lane 寫不同內容
            return "已完成實作"
        if self.role.key == "qa":
            return "驗證: PASS"
        if self.role.key == "senior":
            return "決議: 核可"
        return "ok"


@pytest.mark.asyncio
async def test_merge_conflict_resolved_in_lane(tmp_path, monkeypatch):
    """合併衝突時在 lane 內就地化解 → 保留 lane commit、不走序列化重跑，主幹得到化解後的結果。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", 3)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "NOTES_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    ConflictLaneStub.resolved_calls = 0

    sid = "conflict"
    cwd = workspace.create_workspace(sid)

    async def broadcast(ev):
        pass

    # 兩個獨立任務 → 同一波兩條 lane，皆寫 shared.txt → 第二條合回時衝突。
    main = {
        "pm": MainStub(BY_KEY["pm"], ["任務: #1 甲\n任務: #2 乙", "決議: 完成", "檢討"]),
        "engineer": MainStub(BY_KEY["engineer"], ["x"]),
        "qa": MainStub(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": MainStub(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(sid, broadcast, experts=main, cwd=cwd)
    session._lane_expert_factory = ConflictLaneStub

    result = await session.run("兩件都會改到 shared.txt 的事")

    # 衝突在 lane 內化解 → 兩任務皆 done、最終完成。
    assert all(t["status"] == "done" for t in session._tasks)
    assert result["completed"] is True
    # 指標：化解 1 次、未退回序列化重跑。
    assert session._parallel_metrics["lane_resolved"] == 1
    assert session._parallel_metrics["conflict_retries"] == 0
    assert ConflictLaneStub.resolved_calls == 1
    # 主幹拿到化解後的結果，且 worktree 已清乾淨。
    assert (cwd / "shared.txt").read_text(encoding="utf-8") == "resolved\n"
    assert not (tmp_path / f"{cwd.name}.lanes").exists()


@pytest.mark.asyncio
async def test_merge_lane_blocked_by_worktree_falls_back_not_dropped(tmp_path, monkeypatch):
    """合併被工作樹擋下（未追蹤檔／未提交修改，blocked=True）時，lane 任務應走序列化重跑

    回收，而非像過去那樣被當未知硬失敗靜默丟棄。回歸守門：曾因 git 的「untracked working
    tree files would be overwritten」不含 "CONFLICT" 而落到硬失敗分支，整條 lane 成果消失、
    session 仍帶殘缺產出繼續。
    """
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)

    async def broadcast(ev):
        pass

    session = StudioSession("blocked", broadcast, experts={}, cwd=tmp_path / "main")
    session._main_ctx = LaneContext("main", tmp_path / "main", {})

    # 合併一律回 blocked；abort 不該被呼叫（無 MERGE_HEAD）；重跑記錄走主 lane 的任務。
    aborted = []
    reran: list[dict] = []
    monkeypatch.setattr(
        runner,
        "git_merge_worktree",
        lambda *a, **k: _amr(
            runner.MergeResult(
                ok=False,
                conflict=False,
                blocked=True,
                output="error: The following untracked working tree files would be overwritten by merge:\n\tmdtoc/parser.py",
            )
        ),
    )
    monkeypatch.setattr(runner, "git_merge_abort", lambda *a, **k: _amr(aborted.append(a)))

    async def _fake_rerun(ctx, task, plan_ctx):
        reran.append((ctx, task))
        return True

    monkeypatch.setattr(session, "_run_task_in_lane", _fake_rerun)

    lane = LaneContext("task-2", tmp_path / "main.lanes" / "task-2", {}, branch="task-2")
    lane.notes_buffer.append("半完成筆記")
    lr = LaneResult(ctx=lane, tasks=[{"id": 2, "title": "實作 parser.py"}], ok=True)

    ok = await session._merge_lane(lr, "plan-ctx")

    assert ok is True, "blocked 經序列化重跑回收後應視為成功"
    assert [t["id"] for _c, t in reran] == [2], "lane 的任務應在主 lane 序列化重跑"
    assert all(c is session._main_ctx for c, _t in reran), "重跑須落在主工作樹（main_ctx）"
    assert aborted == [], "blocked 無 MERGE_HEAD，不該呼叫 git merge --abort"
    assert session._parallel_metrics.get("merge_blocked") == 1
    assert session._parallel_metrics.get("conflict_retries") == 1
    assert lane.notes_buffer == [], "改以序列化重跑為準，lane 中途筆記應清空"


async def _amr(value):
    """把同步值包成 awaitable，給 monkeypatch 替換 async runner 函式用。"""
    return value


@pytest.mark.asyncio
async def test_lane_git_snapshot_debug_logs_and_never_throws(tmp_path, monkeypatch, caplog):
    """lane git 快照診斷：DEBUG 開啟時記錄主工作樹 HEAD/狀態/分支是否 reachable；關閉時零成本。

    這是定位「lane 成果漏進主工作樹」根因的儀表，必須安全（任何 git 失敗都不可拖垮主流程）。
    """
    import logging

    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    repo = tmp_path / "main"
    repo.mkdir()
    assert await runner.git_init(repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    assert await runner.git_commit(repo, "base") is not None

    async def broadcast(ev):
        pass

    session = StudioSession("snap", broadcast, experts={}, cwd=repo)

    # DEBUG 關閉（預設 INFO）：不應觸發、不報錯。
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ti.orchestrator"):
        await session._lane_git_snapshot("open", "task-1")
    assert not [r for r in caplog.records if "lane-snapshot" in r.getMessage()]

    # DEBUG 開啟：應記錄且不丟例外（即使分支不存在也安全）。
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="ti.orchestrator"):
        await session._lane_git_snapshot("open", "task-1")  # task-1 不存在 → reachable 安全 False
    snaps = [r.getMessage() for r in caplog.records if "lane-snapshot[open]" in r.getMessage()]
    assert snaps, "DEBUG 等級應記錄 lane-snapshot"
    assert "main_HEAD=" in snaps[0]


class DepStub(LaneStub):
    """記錄各 lane 開工時，其 worktree 是否已看得到前一波合併進來的成果。"""

    saw_prior: dict[str, bool] = {}

    async def speak(self, prompt: str, broadcast) -> str:
        if self.role.key == "engineer":
            # 前一波（task #1）的 lane 把成果寫成 <worktree 目錄名>.txt；lane 分支名現含
            # session 前綴（lane-<sid>-<id>），故第一波產物為 lane-dep-1.txt。
            DepStub.saw_prior[self.cwd.name] = (self.cwd / "lane-dep-1.txt").is_file()
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

    # lane 分支名 = lane-<sid>-<id>；第一波(task#1)開工看不到自己，第二波(task#2)應已看到前波成果。
    assert DepStub.saw_prior.get("lane-dep-1") is False
    assert (
        DepStub.saw_prior.get("lane-dep-2") is True
    ), "第二波 worktree 未從前一波合併後的 HEAD 分支"
    assert {"lane-dep-1.txt", "lane-dep-2.txt"} <= set(workspace.list_files(sid))


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
