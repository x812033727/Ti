"""QA 驗收：任務 #3「lane 收斂 -> final demo」非 LLM await timeout 稽核。

本檔直接釘住產品碼註解與 await 形狀：新增非 LLM await 時，必須同步說明 timeout 來源
或明確標記為已知無界網路 await。行為測試則把 lane stop 掛死，驗證 `_integrate_wave`
透過 `_teardown_lane` 的防爆閥有界收斂。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio import events, orchestrator, runner
from studio.orchestrator import LaneContext, LaneResult, StudioSession
from studio.roles import BY_KEY, Role


def _function_node(fn: Callable[..., Any]) -> ast.AsyncFunctionDef | ast.FunctionDef:
    src = textwrap.dedent(inspect.getsource(fn))
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    return node


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Await):
        return _call_name(node.value)
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ast.unparse(node)


def _await_targets(fn: Callable[..., Any]) -> list[str]:
    return [
        _call_name(node) for node in ast.walk(_function_node(fn)) if isinstance(node, ast.Await)
    ]


def _doc(fn: Callable[..., Any]) -> str:
    text = inspect.getdoc(fn) or ""
    assert text, f"{fn.__qualname__} 必須有 docstring 承載稽核結論"
    return text


def _source(fn: Callable[..., Any]) -> str:
    return textwrap.dedent(inspect.getsource(fn))


def _run_command_exec_timeouts(fn: Callable[..., Any]) -> list[int | str]:
    timeouts: list[int | str] = []
    for node in ast.walk(_function_node(fn)):
        if not isinstance(node, ast.Call):
            continue
        if not _call_name(node.func).endswith("run_command_exec"):
            continue
        timeout = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
        assert timeout is not None, f"{fn.__qualname__} 的 run_command_exec 缺 timeout"
        if isinstance(timeout, ast.Constant):
            timeouts.append(timeout.value)
        else:
            timeouts.append(ast.unparse(timeout))
    return timeouts


def test_integrate_wave_comment_classifies_current_await_targets():
    """`_integrate_wave` 新增 await 時，必須同步更新 #3 程式註解。"""
    assert set(_await_targets(StudioSession._integrate_wave)) == {
        "self.broadcast",
        "self._teardown_lane",
        "self._merge_lane",
        "self._run_task_in_lane",
    }

    doc = _doc(StudioSession._integrate_wave)
    for token in (
        "【#3 過渡段非 LLM await 稽核】",
        "`broadcast`",
        "無界網路 await",
        "`_teardown_lane`",
        "_TEARDOWN_LANE_TIMEOUT",
        "`_merge_lane`",
        "run_command_exec",
        "asyncio.wait_for(communicate(), timeout)",
        "`_flush_lane_notes`",
        "同步函式，無 await",
        "`_run_task_in_lane`",
        "TURN_IDLE_TIMEOUT",
        "TURN_HARD_TIMEOUT",
    ):
        assert token in doc


def test_merge_lane_comment_and_runner_git_timeouts_match():
    """合併鏈的 git await 必須委派 runner，且 runner 端 timeout 要是顯式值。"""
    assert set(_await_targets(StudioSession._merge_lane)) == {
        "self._lane_git_snapshot",
        "runner.git_merge_worktree",
        "runner.git_head_short",
        "self.broadcast",
        "runner.git_merge_abort",
        "self._resolve_conflict_in_lane",
        "self._serialize_lane_rerun",
    }

    doc = _doc(StudioSession._merge_lane)
    for token in (
        "【#3 非 LLM await timeout 來源】",
        "_lane_git_snapshot",
        "git_merge_worktree",
        "timeout=60",
        "git_head_short",
        "timeout=20",
        "git_merge_abort",
        "broadcast",
        "無本地 wait_for",
        "_resolve_conflict_in_lane / _serialize_lane_rerun",
        "TURN timeout",
    ):
        assert token in doc

    assert _run_command_exec_timeouts(runner.git_merge_worktree) == [60]
    assert _run_command_exec_timeouts(runner.git_head_short) == [20]
    assert _run_command_exec_timeouts(runner.git_merge_abort) == [20]
    assert _run_command_exec_timeouts(runner.git_merge_ref_into) == [60]
    assert _run_command_exec_timeouts(runner.git_conflict_markers_present) == [20]
    assert _run_command_exec_timeouts(runner.git_commit) == [30, 30, 20, 20, 20]


def test_teardown_snapshot_and_flush_timeout_contracts_are_pinned():
    """teardown/git snapshot/flush 是 #3 指名範圍，不能靠隱含假設。"""
    assert set(_await_targets(StudioSession._teardown_lane)) == {
        "self.broadcast",
        "asyncio.wait",
        "runner.git_worktree_remove",
        "self._lane_git_snapshot",
    }
    teardown_src = _source(StudioSession._teardown_lane)
    assert "_TEARDOWN_LANE_TIMEOUT" in teardown_src
    assert "asyncio.timeout_at(deadline)" in teardown_src
    assert "timeout=30/20" in teardown_src
    assert "timeout=20" in teardown_src

    assert _run_command_exec_timeouts(runner.git_worktree_remove) == [30, 20]
    assert _run_command_exec_timeouts(StudioSession._lane_git_snapshot) == [20, 20]

    assert not inspect.iscoroutinefunction(StudioSession._flush_lane_notes)
    assert not any(
        isinstance(node, ast.Await)
        for node in ast.walk(_function_node(StudioSession._flush_lane_notes))
    )


def test_stage_demo_and_final_demo_timeout_sources_are_documented():
    """demo 前置只委派 `_final_demo`；真正 demo subprocess 由 runner timeout 收斂。"""
    assert _await_targets(StudioSession._stage_demo) == ["self._final_demo"]
    assert set(_await_targets(StudioSession._final_demo)) == {
        "self.broadcast",
        "runner.run_http_demo",
        "runner.run_command",
    }

    doc = _doc(StudioSession._final_demo)
    for token in (
        "【#3 非 LLM await timeout 來源】",
        "run_http_demo",
        "timeout=config.DEMO_TIMEOUT",
        "wait_for",
        "run_command",
        "killpg",
        "broadcast",
        "無界網路 await",
    ):
        assert token in doc

    http_src = _source(runner.run_http_demo)
    assert "timeout = timeout or config.DEMO_TIMEOUT" in http_src
    assert "kill_process_group(proc)" in http_src
    assert "asyncio.wait_for(proc.wait(), timeout=10)" in http_src
    assert "asyncio.wait_for(drain_task, timeout=5)" in http_src

    run_src = _source(runner.run_command)
    assert "timeout = timeout or config.DEMO_TIMEOUT" in run_src
    assert "return await _finalize_proc(proc, command, timeout)" in run_src


class _ReleasableHangingStopExpert:
    """stop() 永不返回，直到測試用 release 釋放；禁真 sleep。"""

    def __init__(self, role: Role):
        self.role = role
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def stop(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.finished.set()


def _session(experts: dict[str, Any]) -> tuple[StudioSession, list[events.StudioEvent]]:
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    session = StudioSession("qa-transition", broadcast, experts=experts, cwd=None)
    session._main_ctx = LaneContext("main", None, experts, None)
    return session, bucket


def _phase_names(bucket: list[events.StudioEvent]) -> list[str]:
    return [ev.payload.get("phase") for ev in bucket if ev.type == events.EventType.PHASE_CHANGE]


async def test_integrate_wave_normal_lane_teardown_hang_is_bounded(monkeypatch):
    """正常 lane 合併後的 teardown 掛死時，整合段仍在小上界內返回。"""
    monkeypatch.setattr(orchestrator, "_TEARDOWN_LANE_TIMEOUT", 0.05)
    expert = _ReleasableHangingStopExpert(BY_KEY["engineer"])
    experts = {"engineer": expert}
    session, bucket = _session(experts)
    ctx = LaneContext("lane-1", None, experts, None, branch="lane-1")
    tasks = [{"id": 1, "title": "t", "status": "doing"}]
    merged: list[str] = []

    async def merge_lane(lr: LaneResult, plan_ctx: str) -> bool:
        merged.append(lr.ctx.lane_id)
        return True

    session._merge_lane = merge_lane  # type: ignore[method-assign]

    try:
        t0 = time.monotonic()
        ok = await asyncio.wait_for(
            session._integrate_wave(
                [(ctx, tasks)],
                [LaneResult(ctx=ctx, tasks=tasks, ok=True)],
                [],
                "plan",
            ),
            timeout=1.0,
        )
        elapsed = time.monotonic() - t0

        assert ok is True
        assert elapsed < 0.5
        assert merged == ["lane-1"]
        assert expert.started.is_set()
        assert "清理" in _phase_names(bucket)
    finally:
        expert.release.set()
        await asyncio.wait_for(expert.finished.wait(), timeout=1.0)


async def test_integrate_wave_crashed_lane_teardown_hang_is_bounded(monkeypatch):
    """崩潰 lane 丟棄 worktree/筆記時，也不能被 teardown 掛死拖住後續主幹重跑。"""
    monkeypatch.setattr(orchestrator, "_TEARDOWN_LANE_TIMEOUT", 0.05)
    expert = _ReleasableHangingStopExpert(BY_KEY["engineer"])
    experts = {"engineer": expert}
    session, bucket = _session(experts)
    ctx = LaneContext("lane-1", None, experts, None, branch="lane-1")
    tasks = [{"id": 1, "title": "t", "status": "doing"}]
    rerun: list[str] = []

    async def run_task(ctx: LaneContext, task: dict, plan_ctx: str) -> bool:
        rerun.append(f"{ctx.lane_id}:{task['id']}")
        return True

    session._run_task_in_lane = run_task  # type: ignore[method-assign]

    try:
        t0 = time.monotonic()
        ok = await asyncio.wait_for(
            session._integrate_wave([(ctx, tasks)], [RuntimeError("boom")], [], "plan"),
            timeout=1.0,
        )
        elapsed = time.monotonic() - t0

        assert ok is True
        assert elapsed < 0.5
        assert rerun == ["main:1"]
        assert expert.started.is_set()
        assert "清理" in _phase_names(bucket)
    finally:
        expert.release.set()
        await asyncio.wait_for(expert.finished.wait(), timeout=1.0)


def test_no_sdk_upgrade_in_scope():
    """範圍守門：本任務不藉由升 claude-agent-sdk 解 hang。"""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "claude-agent-sdk>=0.1.0" in pyproject
