"""stale-reaper 不得誤收旁路線當前任務(2026-07-11 灰度首航實證競態)。

事故:旁路 claim_next 標 in_progress 但不蓋 session_id,調查管線也從不把 sid 寫回
backlog;主迴圈邊界的 _recover_stale_in_progress 看到「in_progress + session None」,
在認領後 19ms 就把 #300 誤收成 pending——backlog 與實際執行狀態分裂,主迴圈可能
重複認領同一任務。

守護不變量:
- reaper 跳過 _sideline_task_info 指到的任務(留在 in_progress);
- 豁免帶齡上限:info 超齡(旁路懸掛、永不清空)視同孤兒照收,不得永久釘死;
- 旁路未在跑(info=None,含跑完清空後)時,無 session 的 in_progress 照舊回收;
- info 指向別的任務時,其他 stale 任務照收,不得順帶豁免。
"""

from __future__ import annotations

import time

import pytest

from studio import autopilot, backlog, config


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(autopilot.history, "busy_sessions", lambda *_a, **_k: [])
    monkeypatch.setattr(autopilot.history, "sweep_stale_running", lambda *_a, **_k: None)
    return tmp_path


def _claimed_task(title: str) -> dict:
    """仿旁路認領:claim_next 標 in_progress、無 session_id。"""
    backlog.add(title)
    t = backlog.claim_next(lambda _t: True)
    assert t is not None and t.get("session_id") is None
    return t


def _sideline_info(t: dict, *, age_s: float = 0.0) -> dict:
    return {"task_id": t["id"], "title": t["title"], "started_at": time.time() - age_s}


def test_reaper_skips_sideline_current_task(monkeypatch):
    t = _claimed_task("調查:旁路正在跑")
    monkeypatch.setattr(autopilot, "_sideline_task_info", _sideline_info(t))
    autopilot._recover_stale_in_progress()
    got = [x for x in backlog.list_tasks("in_progress") if x["id"] == t["id"]]
    assert got, "旁路當前任務不得被 stale 回收(認領~history session 建立間是裸奔窗口)"


def test_reaper_recovers_overaged_sideline_task(monkeypatch):
    t = _claimed_task("調查:旁路懸掛中")
    over = max(2 * config.AUTOPILOT_INVESTIGATION_TIMEOUT, 3600) + 1
    monkeypatch.setattr(autopilot, "_sideline_task_info", _sideline_info(t, age_s=over))
    autopilot._recover_stale_in_progress()
    got = [x for x in backlog.list_tasks("pending") if x["id"] == t["id"]]
    assert got, "旁路懸掛(info 超齡未清)時豁免須失效,任務不得被永久釘死 in_progress"


def test_reaper_still_recovers_when_no_sideline(monkeypatch):
    t = _claimed_task("孤兒任務")
    monkeypatch.setattr(autopilot, "_sideline_task_info", None)
    autopilot._recover_stale_in_progress()
    got = [x for x in backlog.list_tasks("pending") if x["id"] == t["id"]]
    assert got, "旁路未在跑時,無 session 的 in_progress 照舊回收(原行為)"


def test_reaper_recovers_after_sideline_info_cleared(monkeypatch):
    t = _claimed_task("調查:跑到一半旁路例外退出")
    monkeypatch.setattr(autopilot, "_sideline_task_info", _sideline_info(t))
    autopilot._recover_stale_in_progress()
    assert backlog.list_tasks("in_progress"), "豁免期內不得回收"
    monkeypatch.setattr(autopilot, "_sideline_task_info", None)
    autopilot._recover_stale_in_progress()
    got = [x for x in backlog.list_tasks("pending") if x["id"] == t["id"]]
    assert got, "info 清空(旁路 finally)後同一任務須恢復可回收,豁免不得殘留"


def test_reaper_only_exempts_the_sideline_task(monkeypatch):
    stale = _claimed_task("孤兒任務")
    running = _claimed_task("調查:旁路正在跑")
    monkeypatch.setattr(autopilot, "_sideline_task_info", _sideline_info(running))
    autopilot._recover_stale_in_progress()
    assert [x["id"] for x in backlog.list_tasks("in_progress")] == [running["id"]]
    assert [x["id"] for x in backlog.list_tasks("pending")] == [stale["id"]]
