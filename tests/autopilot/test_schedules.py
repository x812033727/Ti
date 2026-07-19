"""排程任務(Kimi 化 PR10):recurrence 驗證/occurrence 去重/到期入列/CRUD。

守護不變量:
- occurrence key:daily=UTC 日(時刻未到=None)、weekly=僅該 weekday、interval=epoch 桶;
  同 occurrence 重跑 tick 不重複入列(last_fired_key 落盤)。
- 同標題任務尚未消化 → 本次跳過但 key 照記(不堆積)。
- disabled 不觸發;壞排程只 log 不擴散;CRUD 驗證(空標題/壞 recurrence 拒收)。
"""

from __future__ import annotations

import calendar

import pytest

from studio import backlog, config, schedules


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def _utc(y, mo, d, h, mi):
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


def test_occurrence_key_daily_weekly_interval():
    daily = {"recurrence": {"kind": "daily", "time": "08:30"}}
    assert schedules.occurrence_key(daily, _utc(2026, 7, 20, 8, 29)) is None, "時刻未到"
    assert schedules.occurrence_key(daily, _utc(2026, 7, 20, 8, 30)) == "d-20260720"
    assert schedules.occurrence_key(daily, _utc(2026, 7, 21, 23, 0)) == "d-20260721"

    # 2026-07-20 是週一(weekday=0)
    weekly = {"recurrence": {"kind": "weekly", "time": "09:00", "weekday": 0}}
    assert schedules.occurrence_key(weekly, _utc(2026, 7, 20, 9, 0)) == "w-20260720"
    assert schedules.occurrence_key(weekly, _utc(2026, 7, 21, 9, 0)) is None, "非該 weekday"

    iv = {"recurrence": {"kind": "interval_hours", "hours": 6}}
    k1 = schedules.occurrence_key(iv, 6 * 3600 * 100)
    k2 = schedules.occurrence_key(iv, 6 * 3600 * 100 + 5 * 3600)
    k3 = schedules.occurrence_key(iv, 6 * 3600 * 101)
    assert k1 == k2 and k1 != k3, "同桶同 key、跨桶換 key"


def test_validate_recurrence():
    assert schedules.validate_recurrence({"kind": "daily", "time": "23:59"}) == ""
    assert schedules.validate_recurrence({"kind": "daily", "time": "24:00"})
    assert schedules.validate_recurrence({"kind": "weekly", "time": "08:00", "weekday": 7})
    assert schedules.validate_recurrence({"kind": "interval_hours", "hours": 0})
    assert schedules.validate_recurrence({"kind": "cron"})
    assert schedules.validate_recurrence(None)


def test_enqueue_due_dedup_and_backlog(monkeypatch):
    sched, err = schedules.create(
        "每日健檢", "檢查各服務", {"kind": "daily", "time": "08:00"}, priority=0
    )
    assert err == "" and sched["enabled"]
    now = _utc(2026, 7, 20, 8, 5)
    assert schedules.enqueue_due(now) == 1
    tasks = backlog.list_tasks()
    assert len(tasks) == 1 and tasks[0]["title"] == "[排程] 每日健檢"
    assert tasks[0]["source"] == "schedule" and tasks[0]["priority"] == 0
    # 同 occurrence 重跑不重複
    assert schedules.enqueue_due(now + 600) == 0
    assert len(backlog.list_tasks()) == 1
    # 次日新 occurrence;前一筆尚未消化(同標題 pending)→ 跳過但 key 照記
    n = schedules.enqueue_due(_utc(2026, 7, 21, 8, 5))
    assert n == 0 and len(backlog.list_tasks()) == 1
    assert schedules.list_schedules()[0]["last_fired_key"] == "d-20260721", "跳過也記 key"
    # 消化掉後,下一個 occurrence 正常入列
    backlog.set_status(backlog.list_tasks()[0]["id"], "done")
    assert schedules.enqueue_due(_utc(2026, 7, 22, 8, 5)) == 1


def test_disabled_and_bad_schedule_isolated():
    s1, _ = schedules.create("關掉的", "", {"kind": "daily", "time": "00:00"})
    schedules.update(s1["id"], {"enabled": False})
    s2, _ = schedules.create("好的", "", {"kind": "daily", "time": "00:00"})
    # 手動塞壞資料(recurrence 缺欄):不得擴散
    schedules.update(s2["id"], {"title": "好的"})
    data = schedules._load()
    data["schedules"].insert(
        0, {"id": "bad", "enabled": True, "recurrence": {"kind": "daily"}, "title": None}
    )
    schedules._save(data)
    n = schedules.enqueue_due(_utc(2026, 7, 20, 1, 0))
    assert n == 1, "壞排程隔離,好排程照入列"
    assert backlog.list_tasks()[0]["title"] == "[排程] 好的"


def test_crud_validation_and_delete():
    assert schedules.create("", "", {"kind": "daily", "time": "08:00"})[0] is None
    assert schedules.create("x", "", {"kind": "nope"})[0] is None
    s, _ = schedules.create("x", "", {"kind": "interval_hours", "hours": 2})
    got, err = schedules.update(s["id"], {"recurrence": {"kind": "daily", "time": "99:00"}})
    assert got is None and err
    got, _ = schedules.update(s["id"], {"priority": 9, "type": "bug"})
    assert got["priority"] == 2 and got["type"] == "bug", "priority 夾 0-2"
    assert schedules.update("nope", {"title": "y"})[0] is None
    assert schedules.delete(s["id"]) is True
    assert schedules.delete(s["id"]) is False
    assert schedules.list_schedules() == []


def test_autopilot_hook_throttle_and_safety(monkeypatch):
    from studio import autopilot

    monkeypatch.setattr(autopilot, "_schedules_checked_at", 0.0)
    calls = {"n": 0}

    def fake_enqueue(now):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(schedules, "enqueue_due", fake_enqueue)
    autopilot._maybe_enqueue_schedules(now=1000.0)
    autopilot._maybe_enqueue_schedules(now=1030.0)
    assert calls["n"] == 1, "60 秒節流"

    def boom(now):
        raise OSError("disk")

    monkeypatch.setattr(schedules, "enqueue_due", boom)
    monkeypatch.setattr(autopilot, "_schedules_checked_at", 0.0)
    assert autopilot._maybe_enqueue_schedules(now=5000.0) == 0, "失敗吞掉不影響主迴圈"
