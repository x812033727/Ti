"""成果記分卡測試：finish_session 從事件流推導 per-session 記分卡，
/api/metrics 跨場聚合成功率／平均輪數／退回原因／近期趨勢。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, history


@pytest.fixture(autouse=True)
def _hist_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    yield


def _ev(t, **payload):
    return {"type": t, "payload": payload}


def _record(sid, events, requirement="需求"):
    history.start_session(sid, requirement)
    for ev in events:
        history.record_event(sid, ev)
    return history.finish_session(sid)


# 一場「兩任務、第二任務退回一次後過、Demo 通過」的典型事件流
_TYPICAL = [
    _ev("task_status", id=1, title="A", status="doing"),
    _ev("task_status", id=1, title="A", status="review"),
    _ev("run_result", passed=True, detail="驗證通過"),
    _ev("task_status", id=1, title="A", status="done"),
    _ev("task_status", id=2, title="B", status="doing"),
    _ev("run_result", passed=False, detail="自測 `python main.py`：未通過"),
    _ev("task_status", id=2, title="B", status="review"),
    _ev("run_result", passed=False, detail="驗證未通過"),
    _ev("task_status", id=2, title="B", status="review"),
    _ev("run_result", passed=True, detail="驗證通過"),
    _ev("task_status", id=2, title="B", status="done"),
    _ev("demo_result", passed=True, output="7.0"),
    _ev("done", completed=True, stopped=False),
]


def test_scorecard_derived_on_finish():
    meta = _record("s1", _TYPICAL)
    sc = meta["scorecard"]
    assert sc["tasks_total"] == 2
    assert sc["tasks_done"] == 2
    assert sc["rounds_total"] == 3  # A:1 輪 + B:2 輪
    assert sc["avg_rounds"] == 1.5
    assert sc["first_try_done"] == 1  # 只有 A 一次過
    assert sc["rejects"] == {
        "qa_fail": 1,
        "smoke_fail": 1,
        "gate_veto": 0,
        "critic": 0,
        "stall": 0,
    }
    assert sc["demo_passed"] is True
    assert sc["completed"] is True
    assert sc["duration_s"] >= 0


def test_scorecard_reject_phases_counted():
    events = [
        _ev("task_status", id=1, title="A", status="doing"),
        _ev("task_status", id=1, title="A", status="review"),
        _ev("phase_change", phase="客觀閘門", detail="第 1 輪強制退回"),
        _ev("critic_review", gate="pm", passed=False, text="反對"),
        _ev("phase_change", phase="停滯收斂", detail="提早結束"),
        _ev("huddle", title="A", participants=["pm"], conclusion="換做法"),
        _ev("huddle", title="A", limitation=True),
        _ev("done", completed=False, stopped=False),
    ]
    sc = _record("s2", events)["scorecard"]
    assert sc["rejects"]["gate_veto"] == 1
    assert sc["rejects"]["critic"] == 1
    assert sc["rejects"]["stall"] == 1
    assert sc["huddles"] == 1
    assert sc["huddle_limits"] == 1
    assert sc["completed"] is False
    assert sc["demo_passed"] is None  # 沒跑 demo


def test_scorecard_empty_session():
    sc = _record("s3", [_ev("done", completed=False, stopped=True)])["scorecard"]
    assert sc["tasks_total"] == 0
    assert sc["avg_rounds"] == 0.0
    assert sc["stopped"] is True


# --- /api/metrics 聚合 ---------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def test_metrics_aggregates_scorecard(client):
    _record("a", _TYPICAL)
    _record(
        "b",
        [
            _ev("task_status", id=1, title="A", status="doing"),
            _ev("task_status", id=1, title="A", status="review"),
            _ev("phase_change", phase="停滯收斂", detail="提早結束"),
            _ev("done", completed=False, stopped=False),
        ],
    )
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["n"] == 2
    assert sc["completed_rate"] == 0.5  # a 完成、b 未完成
    assert sc["tasks"]["total"] == 3 and sc["tasks"]["done"] == 2
    assert sc["tasks"]["first_try_rate"] == 0.5  # 2 done 中 1 個一次過
    assert sc["rejects"]["stall"] == 1 and sc["rejects"]["qa_fail"] == 1
    # 不足 10+10 場時 previous 為空，不給趨勢誤導
    assert sc["trend"]["previous"] == {"n": 0}
    assert sc["trend"]["recent"]["n"] == 2


def test_metrics_scorecard_empty(client):
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc == {"n": 0}


def test_metrics_trend_recent_vs_previous(client):
    """前 10 場全失敗、近 10 場全成功 → 趨勢顯示成功率上升。"""
    fail = [_ev("done", completed=False, stopped=False)]
    ok = [_ev("done", completed=True, stopped=False)]
    for i in range(10):  # 先記舊的（started_at 較早 → 排序在後）
        _record(f"old{i}", fail)
    for i in range(10):
        _record(f"new{i}", ok)
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["trend"]["recent"]["completed_rate"] == 1.0
    assert sc["trend"]["previous"]["completed_rate"] == 0.0
