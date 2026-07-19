"""第 4 階量測(軌 F2):autonomy 拆解/intent_delivery/stage4 條件卡/source 蓋章。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from studio import backlog, config, improver, insights, interventions, jsonl_log


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def _audit_merged(task_id):
    jsonl_log.append(
        config.AUTOPILOT_STATE_DIR / "audit.jsonl",
        {"task_id": task_id, "outcome": "merged", "ts": time.time()},
    )


def test_autonomy_breakdown_and_intent_delivery():
    t_manual = backlog.add("人工任務", "", source="manual")
    t_intent = backlog.add("意圖任務", "", source="intent")
    t_sched = backlog.add("排程任務", "", source="schedule")
    for t in (t_manual, t_intent, t_sched):
        _audit_merged(t["id"])
    _audit_merged(999999)  # backlog 已不存在的舊任務 → unknown,不進分子分母
    # 排程任務被人工成果審查 → 非零介入,不算 intent_delivery
    interventions.record("task_action", "output_review", task_id=t_sched["id"])
    m = insights.trust_metrics(7)
    a = m["autonomy"]
    assert a["by_source"] == {"manual": 1, "intent": 1, "schedule": 1, "unknown": 1}
    assert a["human"] == 1 and a["autonomous"] == 2
    assert a["autonomous_rate"] == round(2 / 3, 3)
    assert a["intent_delivery"] == 1, "只有零介入的 intent/schedule 源才算"


def test_stage4_conditions_and_promotion(monkeypatch):
    monkeypatch.setattr(config, "INTENT_LOOP", True)
    monkeypatch.setattr(config, "DEPLOY_VERIFY", True)
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "1")
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.8)
    monkeypatch.setattr(config, "EXPERT_SKILLS", True)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_PARALLEL", True)
    monkeypatch.setattr(config, "NORMS_LOOP", True)
    monkeypatch.setattr(config, "CLARIFY_ASYNC", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "c")
    green = {
        "merged": 12,
        "zero_touch": 12,
        "zero_touch_rate": 1.0,
        "interventions": {"total": 0, "per_week": 0.0, "by_category": {}},
        "events": {"deploy_verify_failed": 0},
        "autonomy": {"autonomous_rate": 0.9, "intent_delivery": 2},
    }
    monkeypatch.setattr(insights, "trust_metrics", lambda days=7, state_dir=None: green)
    monkeypatch.setattr(insights, "stage_streak", lambda state_dir=None: 14)
    out = insights.stage_readiness()
    s4 = {c["key"]: c for c in out["stage4_conditions"]}
    assert s4["intent_loop_on"]["ok"] and s4["autonomous_delivery"]["ok"]
    assert s4["deploy_verify_green"]["ok"]
    assert out["stage"] == "4-progress", "第 3 階可宣告+第 4 階全綠 → AI 原生進行式"

    green["autonomy"]["intent_delivery"] = 0
    out = insights.stage_readiness()
    assert out["stage"] == "3-ready", "第 4 階條件缺 → 停在待宣告"
    assert out["stage4_conditions"][1]["ok"] is False

    # deploy_verify 失敗紀錄 → 條件轉紅
    green["autonomy"]["intent_delivery"] = 2
    green["events"] = {"deploy_verify_failed": 1}
    out = insights.stage_readiness()
    assert {c["key"]: c for c in out["stage4_conditions"]}["deploy_verify_green"]["ok"] is False


def test_discovery_source_stamp(monkeypatch):
    stub = SimpleNamespace(_intent_context=lambda: "")
    assert improver.ProjectImprover._discovery_source(stub) == "eval"
    stub = SimpleNamespace(_intent_context=lambda: "【專案常駐意圖】…")
    assert improver.ProjectImprover._discovery_source(stub) == "intent"
