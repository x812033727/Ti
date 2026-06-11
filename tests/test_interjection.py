"""回歸：人類插話的回顯時機與並行 lane 即時生效。

歷史症狀（2026-06-10）：「插話好像沒有用」。前端送出插話後本地不回顯，「你（插話）」氣泡只在
server broadcast `human_message` 時才畫；而並行模式只在波次邊界 drain → 期間畫面毫無反應，且
波次中途送的插話到不了正在跑的 lane（lane 只讀波次開始的 _pending_human 快照）。

修法：
1. ws._pump_interventions 收到插話即 broadcast human_message（即時回顯）。
2. drain 端（_human_prefix / 波次邊界）不再重複 broadcast，只負責把文字餵給專家。
3. _lane_human_prefix 在每個任務再 drain 新插話累加到 _pending_human，波次中途插話即時生效。
"""

from __future__ import annotations

import asyncio

import pytest

from studio.orchestrator import LaneContext, StudioSession


def _session_with_queue():
    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    q: asyncio.Queue[str] = asyncio.Queue()
    session = StudioSession("sid", broadcast, intervention_queue=q)
    return session, q, bucket


@pytest.mark.asyncio
async def test_human_prefix_drains_without_broadcasting():
    """drain 端只回傳前綴文字、不再 broadcast（回顯改由收到插話時負責，避免雙重氣泡）。"""
    session, q, bucket = _session_with_queue()
    q.put_nowait("改用 SQLite")

    prefix = await session._human_prefix()

    assert "改用 SQLite" in prefix
    assert "【使用者插話" in prefix
    assert bucket == [], f"_human_prefix 不應再 broadcast：{bucket}"


@pytest.mark.asyncio
async def test_parallel_lane_picks_up_midwave_interjection():
    """並行 lane 在每個任務 drain 新插話：波次跑到一半送的插話也進得來、累加生效。"""
    session, q, bucket = _session_with_queue()
    lane = LaneContext(lane_id="task-1", cwd=None, experts={}, branch="task-1")

    # 波次開始時無插話 → 前綴為空。
    assert await session._lane_human_prefix(lane) == ""

    # 波次跑到一半，使用者插話進佇列 → 下一個任務的前綴就帶進來。
    q.put_nowait("記得加單元測試")
    p1 = await session._lane_human_prefix(lane)
    assert "記得加單元測試" in p1

    # 再來一則 → 累加（前一則不丟失）。
    q.put_nowait("錯誤要記 log")
    p2 = await session._lane_human_prefix(lane)
    assert "記得加單元測試" in p2 and "錯誤要記 log" in p2

    # _lane_human_prefix 不負責 broadcast（回顯在 ws 收到時做）。
    assert bucket == []


@pytest.mark.asyncio
async def test_concurrent_lane_drains_consume_queue_once_each():
    """多 lane 並行各自 drain：佇列被取空，每則插話只進共享 _pending_human 一次（不重複、不遺漏）。

    _pending_human 為 session 級共享欄位——某 lane drain 後，全部 lane 的前綴都帶到同一份累積
    插話（插話廣播給每條 lane，非只進一條，這正是預期）。要守住的是 get_nowait 原子取出下：
    每則恰一次、佇列取空。
    """
    session, q, bucket = _session_with_queue()
    for i in range(20):
        q.put_nowait(f"指示{i}")
    lanes = [
        LaneContext(lane_id=f"task-{i}", cwd=None, experts={}, branch=f"task-{i}") for i in range(5)
    ]

    await asyncio.gather(*(session._lane_human_prefix(ln) for ln in lanes))

    assert q.empty(), "並行 drain 後佇列應被取空"
    # 每則插話在共享累積緩衝中恰出現一次（無重複消費、無遺漏）。逐行精確比對，避免子字串撞號。
    lines = session._pending_human.split("\n")
    assert sorted(lines) == sorted(f"指示{i}" for i in range(20)), f"插話重複或遺漏：{lines}"


@pytest.mark.asyncio
async def test_pump_interventions_echoes_immediately():
    """ws._pump_interventions 收到 interject 立即 broadcast human_message（即時回顯）。"""
    from studio import ws

    session, q, bucket = _session_with_queue()

    class _FakeWS:
        def __init__(self, msgs):
            self._msgs = list(msgs)

        async def receive_json(self):
            if self._msgs:
                return self._msgs.pop(0)
            # 訊息發完後永久 pending，讓 run_task 先完成、迴圈收束。
            await asyncio.sleep(3600)

    async def _run():
        await asyncio.sleep(0.05)  # 給插話一點時間先被處理

    run_task = asyncio.ensure_future(_run())
    fake_ws = _FakeWS([{"type": "interject", "text": "優先做登入"}])

    await ws._pump_interventions(fake_ws, session, q, run_task)
    await run_task

    human_msgs = [e for e in bucket if e.to_dict().get("type") == "human_message"]
    assert human_msgs, "收到插話應立即 broadcast human_message"
    assert human_msgs[0].to_dict()["payload"]["text"] == "優先做登入"
    # 同時也已入列，供專家於下一次 drain 納入。
    assert q.get_nowait() == "優先做登入"
