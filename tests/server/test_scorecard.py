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
    assert sc["qa_total"] == 3  # 自測 run_result 不計入 QA 分母
    assert sc["qa_pass"] == 2
    assert sc["critic_total"] == 0
    assert sc["critic_pass"] == 0
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
        _ev("critic_review", gate="senior", passed=True, text="放行"),
        _ev("phase_change", phase="停滯收斂", detail="提早結束"),
        _ev("huddle", title="A", participants=["pm"], conclusion="換做法"),
        _ev("huddle", title="A", limitation=True),
        _ev("done", completed=False, stopped=False),
    ]
    sc = _record("s2", events)["scorecard"]
    assert sc["rejects"]["gate_veto"] == 1
    assert sc["rejects"]["critic"] == 1
    assert sc["critic_total"] == 2
    assert sc["critic_pass"] == 1
    assert sc["rejects"]["stall"] == 1
    assert sc["huddles"] == 1
    assert sc["huddle_limits"] == 1
    assert sc["completed"] is False
    assert sc["demo_passed"] is None  # 沒跑 demo


def test_scorecard_counts_rates_from_structured_events_only():
    events = [
        _ev("run_result", passed=True, detail="自測 `python main.py`：通過"),
        _ev("run_result", passed=False, detail="自測 `python main.py`：未通過"),
        _ev("run_result", passed=True, detail="驗證通過"),
        _ev("run_result", passed=False, detail="驗證未通過"),
        _ev("run_result", passed="true", detail="驗證欄位型別錯誤"),
        _ev("critic_review", gate="pm", passed=True, text="放行"),
        _ev("critic_review", gate="senior", passed=False, text="退回"),
        _ev("critic_review", gate="qa", passed="true", text="欄位型別錯誤"),
        _ev("demo_result", passed=False, output="broken"),
        _ev("done", completed=False, stopped=False),
    ]
    sc = _record("strict-counts", events)["scorecard"]
    assert sc["qa_total"] == 3  # 非自測 run_result 都進分母
    assert sc["qa_pass"] == 1  # 僅 passed is True 算通過，不解析字串
    assert sc["critic_total"] == 3
    assert sc["critic_pass"] == 1
    assert sc["rejects"]["smoke_fail"] == 1
    assert sc["rejects"]["qa_fail"] == 1
    assert sc["rejects"]["critic"] == 1
    assert sc["demo_passed"] is False


def test_scorecard_empty_session():
    sc = _record("s3", [_ev("done", completed=False, stopped=True)])["scorecard"]
    assert sc["tasks_total"] == 0
    assert sc["avg_rounds"] == 0.0
    assert sc["qa_total"] == 0
    assert sc["qa_pass"] == 0
    assert sc["critic_total"] == 0
    assert sc["critic_pass"] == 0
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
            _ev("critic_review", gate="pm", passed=True, text="放行"),
            _ev("critic_review", gate="senior", passed=False, text="退回"),
            _ev("demo_result", passed=False, output="broken"),
            _ev("done", completed=False, stopped=False),
        ],
    )
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["n"] == 2
    assert sc["completed_rate"] == 0.5  # a 完成、b 未完成
    assert sc["tasks"]["total"] == 3 and sc["tasks"]["done"] == 2
    assert sc["tasks"]["first_try_rate"] == 0.5  # 2 done 中 1 個一次過
    assert sc["rejects"]["stall"] == 1 and sc["rejects"]["qa_fail"] == 1
    assert sc["rejects"]["critic"] == 1
    assert sc["qa_pass_rate"] == 0.67  # 2 / 3；自測 run_result 不計分母
    assert sc["critic_pass_rate"] == 0.5  # 1 / 2
    assert sc["demo_pass_rate"] == 0.5  # 1 / 2；沒有 demo_result 的場次不計分母
    # 不足 10+10 場時 previous 為空，不給趨勢誤導
    assert sc["trend"]["previous"] == {"n": 0}
    assert sc["trend"]["recent"]["n"] == 2


def test_metrics_scorecard_empty(client):
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc == {
        "n": 0,
        "qa_pass_rate": None,
        "critic_pass_rate": None,
        "demo_pass_rate": None,
    }


def test_metrics_scorecard_rates_none_when_no_denominator(client):
    _record("no-rates", [_ev("done", completed=True, stopped=False)])
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["n"] == 1
    assert sc["qa_pass_rate"] is None
    assert sc["critic_pass_rate"] is None
    assert sc["demo_pass_rate"] is None


def test_metrics_pass_rates_sum_counts_before_dividing(client):
    _record(
        "small",
        [
            _ev("run_result", passed=True, detail="驗證通過"),
            _ev("critic_review", gate="pm", passed=True, text="放行"),
            _ev("demo_result", passed=True),
            _ev("done", completed=True, stopped=False),
        ],
    )
    _record(
        "large",
        [
            _ev("run_result", passed=True, detail="驗證通過"),
            _ev("run_result", passed=False, detail="驗證未通過 1"),
            _ev("run_result", passed=False, detail="驗證未通過 2"),
            _ev("run_result", passed=False, detail="驗證未通過 3"),
            _ev("critic_review", gate="pm", passed=True, text="放行"),
            _ev("critic_review", gate="senior", passed=True, text="放行"),
            _ev("critic_review", gate="qa", passed=True, text="放行"),
            _ev("critic_review", gate="dev", passed=False, text="退回"),
            _ev("demo_result", passed=False),
            _ev("done", completed=False, stopped=False),
        ],
    )
    _record("no-demo", [_ev("done", completed=False, stopped=True)])
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["qa_pass_rate"] == 0.4  # (1 + 1) / (1 + 4)，不是 (1.0 + 0.25) / 2
    assert sc["qa_pass_rate"] != 0.63
    assert sc["critic_pass_rate"] == 0.8  # (1 + 3) / (1 + 4)
    assert sc["critic_pass_rate"] != 0.88
    assert sc["demo_pass_rate"] == 0.5  # 無 demo_result 的場次不進分母


def test_metrics_scorecard_legacy_meta_missing_rate_fields(client):
    meta = history.start_session("legacy", "舊場次")
    meta["status"] = "completed"
    meta["scorecard"] = {
        "tasks_total": 1,
        "tasks_done": 1,
        "rounds_total": 1,
        "avg_rounds": 1.0,
        "first_try_done": 1,
        "rejects": {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0},
        "demo_passed": True,
        "completed": True,
        "stopped": False,
    }
    history._write_meta("legacy", meta)
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["n"] == 1
    assert sc["qa_pass_rate"] is None
    assert sc["critic_pass_rate"] is None
    assert sc["demo_pass_rate"] == 1.0


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
