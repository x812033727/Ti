"""QA for 任務 #2：`_aggregate_scorecard` 跨場聚合測試通過率、審查通過率，
並補上目前完全缺失的 Demo 通過率（demo_passed 為 True 的場次佔有 demo 場次比例）。

驗收對應：
- 標準 2：`/api/metrics` 的 `scorecard` 回傳含 qa_pass_rate / critic_pass_rate / demo_pass_rate，
  分母為 0 時回傳 None，不丟例外。
- 標準 5：舊 meta.json 缺新欄位時，聚合走 `.get()` 防守不崩。
- 範圍守門：跨場是「加總後除」而非「平均的平均」，避免小場次放大失真。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, history, routes


def _ev(t, **payload):
    return {"type": t, "payload": payload}


def _record(sid, events, requirement="需求"):
    history.start_session(sid, requirement)
    for ev in events:
        history.record_event(sid, ev)
    return history.finish_session(sid)


@pytest.fixture(autouse=True)
def _hist_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    yield


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


# --- Demo 通過率跨場聚合 ---------------------------------------------


def test_demo_pass_rate_aggregates_using_session_level(client):
    """單場 `demo_passed` 為 bool | None；聚合層以「場次維度」聚合，
    只把 demo_passed is not None 的場次納入分母，None 場次不計。"""
    # A：demo 通過；B：demo 失敗；C：根本沒跑 demo（None）
    _record(
        "demo-a",
        [_ev("demo_result", passed=True), _ev("done", completed=True, stopped=False)],
    )
    _record(
        "demo-b",
        [_ev("demo_result", passed=False), _ev("done", completed=False, stopped=False)],
    )
    _record(
        "demo-c",
        [_ev("done", completed=False, stopped=True)],  # 無 demo_result
    )
    sc = client.get("/api/metrics").json()["scorecard"]
    # 有 demo 的場次：A、B 共 2 場；其中僅 A 通過 → 1/2 = 0.5
    assert sc["demo_pass_rate"] == 0.5
    assert sc["n"] == 3  # 三場都進入聚合（不論 demo 是否存在）


def test_demo_pass_rate_none_when_no_demo_in_any_session(client):
    """所有場次都沒有 demo_result → demo 通過率回 None（不是 0，避免被誤讀為「全失敗」）。"""
    _record("no-demo-1", [_ev("done", completed=True, stopped=False)])
    _record("no-demo-2", [_ev("done", completed=False, stopped=True)])
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["demo_pass_rate"] is None


# --- 跨場加總後除（非平均的平均）--------------------------------------


def test_qa_pass_rate_is_sum_then_divide_not_average_of_averages(client):
    """聚合必須 sum 後除，避免小場次放大失真。
    場次 A：1 QA、1 pass（小場，rate=1.0）
    場次 B：4 QA、1 pass（大場，rate=0.25）
    → 跨場正確：2/5 = 0.4
    → 若誤用平均：(1.0 + 0.25) / 2 = 0.625（小場放大失真，必須不是這個值）"""
    # A：1 個非自測 run_result 通過
    _record(
        "qa-a",
        [
            _ev("task_status", id=1, title="x", status="review"),
            _ev("run_result", passed=True, detail="QA ok"),
            _ev("done", completed=True, stopped=False),
        ],
    )
    # B：4 個非自測 run_result（1 通過 + 3 失敗）
    runs = [_ev("run_result", passed=False, detail=f"QA fail {i}") for i in range(3)]
    _record(
        "qa-b",
        [
            _ev("task_status", id=1, title="y", status="review"),
            _ev("run_result", passed=True, detail="QA ok"),
            *runs,
            _ev("done", completed=False, stopped=False),
        ],
    )
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["qa_pass_rate"] == 0.4
    # 反向防護：絕對不能是平均的平均值
    assert sc["qa_pass_rate"] != 0.63  # 0.625 四捨五入


def test_critic_pass_rate_is_sum_then_divide(client):
    """聚合 critic 通過率也是 sum 後除。
    場次 A：1 critic pass（rate=1.0）
    場次 B：3 critic pass + 1 critic fail（rate=0.75）
    → 跨場正確：(1+3) / (1+4) = 4/5 = 0.8"""
    _record(
        "c-a",
        [
            _ev("critic_review", gate="pm", passed=True, text="ok"),
            _ev("done", completed=False, stopped=False),
        ],
    )
    _record(
        "c-b",
        [
            _ev("critic_review", gate="pm", passed=True, text="ok"),
            _ev("critic_review", gate="senior", passed=True, text="ok"),
            _ev("critic_review", gate="qa", passed=True, text="ok"),
            _ev("critic_review", gate="dev", passed=False, text="no"),
            _ev("done", completed=False, stopped=False),
        ],
    )
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["critic_pass_rate"] == 0.8


# --- 三通過率同時存在的混合場景 ----------------------------------------


def test_all_three_pass_rates_in_one_aggregation(client):
    """單一聚合同時算 QA / Demo / Critic 三通過率，三者互不污染。"""
    _record(
        "mix-a",
        [
            # QA：2 通過 / 1 失敗 → 分數 2/3
            _ev("run_result", passed=True, detail="QA ok"),
            _ev("run_result", passed=True, detail="QA ok"),
            _ev("run_result", passed=False, detail="QA fail"),
            # Critic：1 通過 / 1 失敗 → 分數 1/2
            _ev("critic_review", gate="pm", passed=True, text="ok"),
            _ev("critic_review", gate="senior", passed=False, text="no"),
            # Demo 通過
            _ev("demo_result", passed=True),
            _ev("done", completed=True, stopped=False),
        ],
    )
    _record(
        "mix-b",
        [
            # QA：全失敗 → 0/2
            _ev("run_result", passed=False, detail="QA fail 1"),
            _ev("run_result", passed=False, detail="QA fail 2"),
            # Demo 失敗
            _ev("demo_result", passed=False),
            _ev("done", completed=False, stopped=False),
        ],
    )
    sc = client.get("/api/metrics").json()["scorecard"]
    # QA：(2+0)/(3+2) = 0.4
    assert sc["qa_pass_rate"] == 0.4
    # Critic：只有一場有 critic → 1/2
    assert sc["critic_pass_rate"] == 0.5
    # Demo：2 場有 demo，1 通過 → 0.5
    assert sc["demo_pass_rate"] == 0.5


# --- 零場次 / 全零分母邊界 --------------------------------------------


def test_zero_sessions_returns_none_for_all_three_rates(client):
    """完全沒有已結束的 session → 三個通過率都是 None，不是 0。"""
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc == {
        "n": 0,
        "qa_pass_rate": None,
        "critic_pass_rate": None,
        "demo_pass_rate": None,
    }


def test_each_rate_independently_none_when_only_other_data_exists(client):
    """分母各自獨立：只有一場 session 跑了 QA（無 critic、無 demo）→
    qa 算得出、其餘兩個必須 None。"""
    _record(
        "only-qa",
        [
            _ev("run_result", passed=True, detail="QA ok"),
            _ev("run_result", passed=False, detail="QA fail"),
            _ev("done", completed=False, stopped=True),
        ],
    )
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["qa_pass_rate"] == 0.5
    assert sc["critic_pass_rate"] is None
    assert sc["demo_pass_rate"] is None


# --- 舊 meta.json 缺新欄位聚合防守（驗收標準 5）-----------------------


def test_aggregate_handles_legacy_meta_without_new_count_fields(client):
    """模擬「任務 #1 之前已存在的舊 meta.json」：scorecard 內沒有
    qa_total / qa_pass / critic_total / critic_pass 欄位——
    `_aggregate_scorecard` 必須走 `.get()` 防守，不丟例外，且三通過率合理回傳。"""
    # 直接寫入舊格式 meta（繞過 _derive_scorecard 的計算）
    m = history.start_session("legacy-1", "舊場次")
    m["status"] = "completed"
    m["scorecard"] = {
        "tasks_total": 2,
        "tasks_done": 2,
        "rounds_total": 2,
        "avg_rounds": 1.0,
        "first_try_done": 1,
        # 故意只給舊欄位
        "rejects": {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0},
        "demo_passed": True,
        "completed": True,
        "stopped": False,
    }
    history._write_meta("legacy-1", m)

    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["n"] == 1
    # 缺欄位 → qa_total/qa_pass 視為 0 → 通過率 None
    assert sc["qa_pass_rate"] is None
    assert sc["critic_pass_rate"] is None
    # demo_passed 仍存在 → demo 通過率可算：1/1 = 1.0
    assert sc["demo_pass_rate"] == 1.0


def test_aggregate_handles_legacy_meta_with_partial_new_fields(client):
    """舊場次只有部分新欄位（部分缺失），聚合必須不崩、缺的視為 0。"""
    m = history.start_session("legacy-2", "舊場次")
    m["status"] = "completed"
    m["scorecard"] = {
        "tasks_total": 0,
        "tasks_done": 0,
        "rounds_total": 0,
        "avg_rounds": 0.0,
        "first_try_done": 0,
        "qa_total": 2,
        "qa_pass": 1,
        # 缺 critic_total / critic_pass
        "rejects": {"qa_fail": 1, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0},
        "demo_passed": None,
        "completed": False,
        "stopped": True,
    }
    history._write_meta("legacy-2", m)

    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["n"] == 1
    assert sc["qa_pass_rate"] == 0.5  # 1/2
    assert sc["critic_pass_rate"] is None  # 分母 0 → None
    assert sc["demo_pass_rate"] is None  # 無 demo → None


def test_aggregate_unit_call_does_not_crash_on_legacy_meta(tmp_path, monkeypatch):
    """直接呼叫 `_aggregate_scorecard`（不走 HTTP）也要能處理舊 meta。"""
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")

    legacy_sessions = [
        {
            "status": "completed",
            "scorecard": {
                "tasks_total": 0,
                "tasks_done": 0,
                "rounds_total": 0,
                "avg_rounds": 0.0,
                "first_try_done": 0,
                "rejects": {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0},
                "demo_passed": None,
                "completed": False,
                "stopped": True,
            },
        }
    ]
    out = routes._aggregate_scorecard(legacy_sessions)
    assert out["n"] == 1
    assert out["qa_pass_rate"] is None
    assert out["critic_pass_rate"] is None
    assert out["demo_pass_rate"] is None


# --- 分母為 0 與「0%」的語意差異守門 -----------------------------------


def test_denominator_zero_does_not_return_zero_string(client):
    """分母為 0 時回傳 None（不是 0.0）——後續前端 `pct()` 才能顯示 `—` 而不是 `0%`，
    區分『所有測試都失敗』與『根本沒跑過測試』。"""
    _record("zero-denom", [_ev("done", completed=True, stopped=False)])
    sc = client.get("/api/metrics").json()["scorecard"]
    assert sc["qa_pass_rate"] is None
    assert sc["critic_pass_rate"] is None
    assert sc["demo_pass_rate"] is None
    # 明確禁止 None 被誤轉成 0
    assert sc["qa_pass_rate"] != 0
    assert sc["qa_pass_rate"] != 0.0