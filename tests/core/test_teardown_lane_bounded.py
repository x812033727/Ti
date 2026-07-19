"""`_teardown_lane` 有界收斂與可觀測性的離線測試（任務 #2）。

實例 #261：某 expert `stop()` 在 anyio 吞取消下永不返回，使 `_teardown_lane` 靜默卡死 76 分鐘。
本測試以 monkeypatch 把模組常數 `_TEARDOWN_LANE_TIMEOUT` 縮到 0.1s 量級，用永不返回的 stop() 模擬掛死，
斷言 teardown 於上界內收斂、且能區分「有界收斂」vs「等滿上界」；全程 stub、禁真 sleep、秒級跑完。

關鍵守門：#261 真正根因是 **吞取消**（swallows cancellation）的永不返回——對它 `asyncio.timeout`+
`gather`（協作式取消）完全穿不透，唯 `asyncio.wait` 的 abandon-pending 有界。因此除了「可取消」的
掛死 stub，另備 `_CancelSwallowingStopExpert`（`except CancelledError: continue`）作為真根因路徑守門：
此 case 在舊 gather+timeout 實作下會整段 hang（紅），只有正解 abandon-pending 能讓它綠。
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


class _CancelSwallowingStopExpert:
    """stop() **吞取消**且永不返回（#261 真根因）。

    `gather`+`asyncio.timeout` 對它穿不透（cancel 被吞→連外層都被拖死）；唯 `asyncio.wait` 放手
    pending 有界。附 `_release` 逃生門讓測試在斷言後乾淨收尾（以非 cancel 訊號結束，避免洩漏 task 警告）。
    """

    def __init__(self, role: Role):
        self.role = role
        self.started = False
        self._release = asyncio.Event()

    async def stop(self) -> None:
        self.started = True
        while not self._release.is_set():
            try:
                await self._release.wait()
            except asyncio.CancelledError:
                continue  # 吞取消：cancel 無效，只有 _release 被 set 才真正返回

    def release(self) -> None:
        self._release.set()


class _TracingHangingStopExpert:
    """記錄 stop() 是否被啟動，之後永久等待；用來抓串列 teardown。"""

    def __init__(self, role: Role, name: str, trace: list[str]):
        self.role = role
        self.name = name
        self.trace = trace

    async def stop(self) -> None:
        self.trace.append(f"stop:{self.name}")
        await asyncio.Event().wait()  # 永不返回；禁真 sleep


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


async def test_teardown_bounded_when_stop_swallows_cancellation(monkeypatch):
    """#261 真根因守門：stop() **吞取消**永不返回時，teardown 仍靠 abandon-pending 有界收斂。

    此 case 對舊 gather+asyncio.timeout（協作式取消）穿不透而整段 hang；只有正解
    `asyncio.wait` 放手 pending 能讓它在上界內返回。外層 wait_for(5s) 作硬保險：若未來
    有人退回 gather 實作，這裡會超時失敗（而非讓整個測試套件卡死）。
    """
    monkeypatch.setattr(orchestrator, "_TEARDOWN_LANE_TIMEOUT", 0.15)
    ex = _CancelSwallowingStopExpert(BY_KEY["engineer"])
    experts = {"engineer": ex}
    s, bucket = _session(experts)
    ctx = LaneContext("lane-1", None, experts, None)

    try:
        t0 = time.monotonic()
        await asyncio.wait_for(s._teardown_lane(ctx), timeout=5.0)
        elapsed = time.monotonic() - t0

        assert ex.started is True  # stop() 確有被啟動（而非被略過）
        assert elapsed < 2.0  # 有界返回，未被吞取消 hang 拖死
        assert elapsed >= 0.1  # 確係走到 abandon-pending 上界，非提早略過
        assert "清理" in _phase_anchors(bucket)
    finally:
        # 以非 cancel 訊號放行被放手的 stop()，讓背景 task 乾淨收尾（避免洩漏 task 警告）。
        ex.release()
        for _ in range(3):
            await asyncio.sleep(0)


async def test_teardown_parallel_not_linear_in_expert_count(monkeypatch):
    """多個永不返回的 stop() 並行啟動（abandon-pending），且清理錨點必須在 stop 前送出。"""
    monkeypatch.setattr(orchestrator, "_TEARDOWN_LANE_TIMEOUT", 0.2)
    trace: list[str] = []
    experts = {
        f"e{i}": _TracingHangingStopExpert(BY_KEY["engineer"], f"e{i}", trace) for i in range(6)
    }
    critics = {f"c{i}": _TracingHangingStopExpert(BY_KEY["qa"], f"c{i}", trace) for i in range(6)}
    s, bucket = _session(experts)
    ctx = LaneContext("lane-1", None, experts, critics)
    original_broadcast = s.broadcast

    async def broadcast(ev):
        if ev.type == events.EventType.PHASE_CHANGE:
            trace.append(f"phase:{ev.payload.get('phase')}")
        await original_broadcast(ev)

    s.broadcast = broadcast

    t0 = time.monotonic()
    await s._teardown_lane(ctx)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0
    assert trace[0] == "phase:清理"
    assert {item for item in trace if item.startswith("stop:")} == {
        *(f"stop:e{i}" for i in range(6)),
        *(f"stop:c{i}" for i in range(6)),
    }
    assert "清理" in _phase_anchors(bucket)


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
