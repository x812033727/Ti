"""`_teardown_lane` 有界收斂與可觀測性的離線測試（任務 #2）。

實例 #261：某 expert `stop()` 在 anyio 吞取消下永不返回，使 `_teardown_lane` 靜默卡死 76 分鐘。
本測試以 monkeypatch 把模組常數 `_TEARDOWN_LANE_TIMEOUT` 縮到 0.1s 量級，用 `asyncio.Event().wait()`
模擬「永不返回」的 stop()，斷言 teardown 於上界內收斂、且能區分「有界收斂」vs「等滿上界」；
全程 stub、禁真 sleep、秒級跑完。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from studio import events, orchestrator
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class _HangingStopExpert:
    """stop() 永不返回（模擬 anyio 吞取消下 disconnect 掛死）。"""

    def __init__(self, role: Role):
        self.role = role

    async def stop(self) -> None:
        await asyncio.Event().wait()  # 永不返回；禁真 sleep


class _FastStopExpert:
    """stop() 立即返回（kill-first 正常路徑）。"""

    def __init__(self, role: Role):
        self.role = role
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _session(experts: dict) -> tuple[StudioSession, list]:
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    # cwd=None：略過 git worktree 收尾，聚焦 stop() 收斂路徑（實際卡點所在）。
    s = StudioSession("t", broadcast, experts=experts, cwd=None)
    s._main_ctx = LaneContext("main", None, experts, None)
    return s, bucket


def _phase_anchors(bucket: list) -> list[str]:
    return [ev.payload.get("phase") for ev in bucket if ev.type == events.EventType.PHASE_CHANGE]


async def test_teardown_bounded_when_stop_never_returns(monkeypatch):
    """任一 expert stop() 永不返回時，_teardown_lane 於模組常數上界內收斂、不外拋。"""
    monkeypatch.setattr(orchestrator, "_TEARDOWN_LANE_TIMEOUT", 0.15)
    experts = {"engineer": _HangingStopExpert(BY_KEY["engineer"])}
    s, bucket = _session(experts)
    ctx = LaneContext("lane-1", None, experts, None)

    t0 = time.monotonic()
    await s._teardown_lane(ctx)  # 不應拋 TimeoutError
    elapsed = time.monotonic() - t0

    # 有界：接近上界即返回（而非等滿 7200s）。放寬到 2s 容忍排程抖動仍遠小於真實上界。
    assert elapsed < 2.0
    assert elapsed >= 0.1  # 確係走到 timeout 兜底，非提早略過
    # 進入前的「清理」錨點可被 history/watchdog 看見。
    assert "清理" in _phase_anchors(bucket)


async def test_teardown_parallel_not_linear_in_expert_count(monkeypatch):
    """多個永不返回的 stop() 以 gather 並行收掉：最壞時間為單一上界，不隨 expert 數線性放大。"""
    monkeypatch.setattr(orchestrator, "_TEARDOWN_LANE_TIMEOUT", 0.2)
    experts = {f"e{i}": _HangingStopExpert(BY_KEY["engineer"]) for i in range(6)}
    critics = {f"c{i}": _HangingStopExpert(BY_KEY["qa"]) for i in range(6)}
    s, _ = _session(experts)
    ctx = LaneContext("lane-1", None, experts, critics)

    t0 = time.monotonic()
    await s._teardown_lane(ctx)
    elapsed = time.monotonic() - t0

    # 12 個 hanging stop 若串列（Σ）會是 12×0.2=2.4s；並行則 ≈ 單一 0.2s 上界。
    assert elapsed < 1.0


async def test_teardown_fast_path_converges_well_under_bound(monkeypatch):
    """區分「有界收斂」vs「等滿上界」：正常 stop() 應遠早於上界返回，且錨點仍發出。"""
    monkeypatch.setattr(orchestrator, "_TEARDOWN_LANE_TIMEOUT", 5.0)
    experts = {"engineer": _FastStopExpert(BY_KEY["engineer"])}
    s, bucket = _session(experts)
    ctx = LaneContext("lane-1", None, experts, None)

    t0 = time.monotonic()
    await s._teardown_lane(ctx)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0  # 遠小於 5s 上界：正常路徑不靠 timeout 兜底
    assert experts["engineer"].stopped is True
    assert "清理" in _phase_anchors(bucket)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
