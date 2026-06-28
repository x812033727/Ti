"""QA for 任務 #1：在 _derive_scorecard 新增單場『測試通過率／審查通過率』
所需的分子分母計數（qa_total/qa_pass、critic_total/critic_pass）。

驗收範圍：
- 標準 1：_derive_scorecard 回傳 dict 含四個計數欄位，且舊欄位（rejects/demo_passed/
  completed/stopped/tasks_* 等）原樣保留。
- 標準 5：舊 meta.json 缺新欄位時，_aggregate_scorecard 走 .get() 防守不丟例外。
- 標準 6（負面）：不解析自然語言、確定性從事件流推導（不引入 LLM）。

刻意只覆蓋任務 #1 單場推導層；跨場三個 pass_rate 聚合屬於任務 #2，不在本檔斷言。
"""

from __future__ import annotations

import pytest

from studio import config, history, routes


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


# ---------- 標準 1：四個計數欄位確定性從事件流推導 --------------------


def test_qa_counts_only_run_result_non_smoke():
    """qa_total 只計『非自測』的 run_result；qa_pass 只計 passed is True（嚴格型別）。"""
    events = [
        # 自測 1：detail 以「自測」開頭 → 不計入 QA 分母；通過不進 smoke_fail
        _ev("run_result", passed=True, detail="自測 `python main.py`：OK"),
        # QA 1：通過
        _ev("run_result", passed=True, detail="驗證通過"),
        # QA 2：失敗
        _ev("run_result", passed=False, detail="驗證未通過"),
        # QA 3：passed 不是 bool True（字串 "true"）→ 不計入 qa_pass，但計入 qa_total
        _ev("run_result", passed="true", detail="驗證通過"),
        _ev("done", completed=True, stopped=False),
    ]
    sc = _record("qa-only", events)["scorecard"]
    assert sc["qa_total"] == 3, "三個非自測 run_result 都要計入分母"
    assert sc["qa_pass"] == 1, "只有嚴格型別 bool True 才計入分子"
    # 自測通過不污染 qa_*，也不會反映成 smoke_fail
    assert sc["rejects"]["smoke_fail"] == 0
    assert sc["rejects"]["qa_fail"] == 1


def test_critic_counts_include_all_events():
    """critic_review 事件全部計 critic_total，passed is True 計 critic_pass。"""
    events = [
        _ev("critic_review", gate="pm", passed=False, text="反對"),
        _ev("critic_review", gate="senior", passed=True, text="放行"),
        _ev("critic_review", gate="qa", passed="True", text="字串 True → 不算"),  # 嚴格型別
        _ev("critic_review", gate="qa", passed=True, text="OK"),
        _ev("done", completed=False, stopped=False),
    ]
    sc = _record("critic-only", events)["scorecard"]
    assert sc["critic_total"] == 4
    assert sc["critic_pass"] == 2
    assert sc["rejects"]["critic"] == 1  # 既有 rejects 用 truthiness 計退回，語意不變


def test_zero_event_boundary_returns_zero_counts_not_missing():
    """零事件 / 只有 done 事件 → qa_* / critic_* 全為 0，不是 KeyError、不是 None。"""
    sc = _record("empty", [_ev("done", completed=False, stopped=True)])["scorecard"]
    assert sc["qa_total"] == 0
    assert sc["qa_pass"] == 0
    assert sc["critic_total"] == 0
    assert sc["critic_pass"] == 0
    # 確定是 int 0，方便聚合層 sum 後除（避免 None 污染）
    assert isinstance(sc["qa_total"], int)
    assert isinstance(sc["qa_pass"], int)
    assert isinstance(sc["critic_total"], int)
    assert isinstance(sc["critic_pass"], int)


# ---------- 標準 1：舊欄位原樣保留 -------------------------------------


def test_existing_fields_preserved_unchanged():
    """新增計數欄位後，所有舊欄位（rejects/demo_passed/completed/stopped/tasks_*/...）
    仍原樣存在且語意不變。"""
    events = [
        _ev("task_status", id=1, title="A", status="doing"),
        _ev("task_status", id=1, title="A", status="review"),
        _ev("run_result", passed=True, detail="驗證通過"),
        _ev("task_status", id=1, title="A", status="done"),
        _ev("phase_change", phase="客觀閘門", detail="退回"),
        _ev("phase_change", phase="停滯收斂", detail="提早結束"),
        _ev("huddle", title="A", participants=["pm"], conclusion="換做法"),
        _ev("huddle", title="A", limitation=True),
        _ev("demo_result", passed=True, output="7.0"),
        _ev("done", completed=True, stopped=False),
    ]
    sc = _record("preserve", events)["scorecard"]
    # 必須存在且語意正確
    assert sc["tasks_total"] == 1
    assert sc["tasks_done"] == 1
    assert sc["rounds_total"] == 1
    assert sc["avg_rounds"] == 1.0
    assert sc["first_try_done"] == 1
    assert sc["rejects"] == {
        "qa_fail": 0,
        "smoke_fail": 0,
        "gate_veto": 1,
        "critic": 0,
        "stall": 1,
    }
    assert sc["huddles"] == 1
    assert sc["huddle_limits"] == 1
    assert sc["demo_passed"] is True
    assert sc["completed"] is True
    assert sc["stopped"] is False
    assert "duration_s" in sc
    # 新欄位也並存
    assert "qa_total" in sc and "qa_pass" in sc
    assert "critic_total" in sc and "critic_pass" in sc


# ---------- 標準 5：舊 meta 缺新欄位的聚合防守 -------------------------


def test_aggregate_scorecard_legacy_meta_without_new_counts_does_not_crash():
    """模擬『任務 #1 之前已存在的舊 meta.json』：scorecard 裡沒有 qa_total / qa_pass /
    critic_total / critic_pass 欄位——_aggregate_scorecard 必須走 .get() 防守，不丟例外。"""
    # 直接造一份舊格式 meta（沒有新欄位），繞過 _derive_scorecard 的計算
    history.start_session("legacy", "舊需求")
    legacy_meta = {
        "session_id": "legacy",
        "requirement": "舊需求",
        "started_at": 1.0,
        "finished_at": 2.0,
        "status": "completed",
        "n_events": 1,
        # 故意只給舊欄位，不給 qa_total / qa_pass / critic_total / critic_pass
        "scorecard": {
            "tasks_total": 2,
            "tasks_done": 2,
            "rounds_total": 3,
            "avg_rounds": 1.5,
            "first_try_done": 1,
            "rejects": {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0},
            "huddles": 0,
            "huddle_limits": 0,
            "demo_passed": True,
            "completed": True,
            "stopped": False,
            "duration_s": 1.0,
        },
    }
    history._write_meta("legacy", legacy_meta)
    # 聚合必須不丟例外
    agg = routes._aggregate_scorecard(history.list_sessions())
    assert agg["n"] == 1
    # 聚合後的 qa/critic 計數欄位是 0（.get 防守），不會 KeyError
    assert agg.get("qa_total", "missing") == 0 or "qa_total" not in agg
    assert agg.get("qa_pass", "missing") == 0 or "qa_pass" not in agg
    assert agg.get("critic_total", "missing") == 0 or "critic_total" not in agg
    assert agg.get("critic_pass", "missing") == 0 or "critic_pass" not in agg


def test_aggregate_scorecard_missing_scorecard_field_does_not_crash():
    """meta 連 scorecard 欄位都沒有（更舊的版本）→ 聚合直接跳過、不崩。"""
    history.start_session("no-sc", "舊需求")
    bare = {
        "session_id": "no-sc",
        "requirement": "舊需求",
        "started_at": 1.0,
        "status": "completed",
        "n_events": 1,
    }
    history._write_meta("no-sc", bare)
    agg = routes._aggregate_scorecard(history.list_sessions())
    # 沒有 scorecard 的 session 不算進聚合
    assert agg["n"] == 0


# ---------- 標準 6（負面）：沒引入 LLM、沒解析自然語言 -----------------


def test_scorecard_uses_structured_event_flags_only():
    """只讀取事件的 type 與 payload 結構化欄位（detail/pass 等），不導入任何 LLM / NLP。
    這是『靜態證據』測試——以白箱方式檢視 _derive_scorecard 的原始碼沒引入自然語言
    解析依賴。"""
    import inspect

    from studio.history import _derive_scorecard

    src = inspect.getsource(_derive_scorecard)
    # 確定性推導：只該看到結構化欄位讀取與布林/計數判斷
    forbidden = [
        "import openai",
        "import anthropic",
        "import litellm",
        "import google.generativeai",
        ".chat(",
        ".complete(",
        "langchain",
        "llama_index",
    ]
    for tok in forbidden:
        assert tok not in src, f"_derive_scorecard 不該引入自然語言依賴：{tok}"
    # 必須用結構化欄位
    assert "passed" in src
    assert "is_smoke" in src
    assert "startswith" in src  # 自測以 detail 前綴判定，是確定性字串比對
