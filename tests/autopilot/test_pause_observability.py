"""暫停可觀測(穩定強化 α)。

2026-07-10 事故:UI/哨兵檔暫停後,主迴圈空轉但 status.json 凍結在上一筆 running 任務,
53 分鐘死寂被外部監控誤判為「看門狗失效的卡死」並觸發人工重啟。

守護不變量:
- 暫停每輪 `_write_status("paused")`(updated_at 前進,外部可判活著)。
- 進入暫停第一輪:log 一次+收斂殘留 in_progress;之後輪不重複 log/收斂。
- 恢復時 log 一次(冪等);非暫停期間 `_note_resumed` no-op。
"""

from __future__ import annotations

import json

import pytest

from studio import autopilot, config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(autopilot, "_paused_logged", False)

    async def fast_sleep(_s):
        return None

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    return tmp_path


def _status(tmp_path):
    return json.loads((tmp_path / "ap" / "status.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_pause_tick_writes_paused_state_and_advances(tmp_path, monkeypatch):
    recovered = []
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: recovered.append(1))

    await autopilot._pause_tick()
    st1 = _status(tmp_path)
    assert st1["state"] == "paused", "暫停必須寫 state=paused(區分刻意暫停與卡死)"
    assert recovered == [1], "進入暫停第一輪收斂殘留 in_progress"

    await autopilot._pause_tick()
    st2 = _status(tmp_path)
    assert (
        st2["state"] == "paused" and st2["updated_at"] >= st1["updated_at"]
    ), "每輪刷新 updated_at,外部監控可判活著"
    assert recovered == [1], "之後輪不重複收斂"


@pytest.mark.asyncio
async def test_transition_logs_once_each_way(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    import logging

    with caplog.at_level(logging.INFO, logger="ti.autopilot"):
        await autopilot._pause_tick()
        await autopilot._pause_tick()
        autopilot._note_resumed()
        autopilot._note_resumed()

    pauses = [r for r in caplog.records if "已暫停" in r.getMessage()]
    resumes = [r for r in caplog.records if "已恢復" in r.getMessage()]
    assert len(pauses) == 1, "進入暫停只 log 一次(避免每 10s 刷屏)"
    assert len(resumes) == 1, "恢復只 log 一次(冪等)"


def test_note_resumed_noop_when_not_paused(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="ti.autopilot"):
        autopilot._note_resumed()
    assert not any("已恢復" in r.getMessage() for r in caplog.records)
