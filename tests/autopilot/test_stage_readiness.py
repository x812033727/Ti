"""升階儀表(軌 D1):八開關現值/四條件量測/階段判定。"""

from __future__ import annotations

import pytest

from studio import backlog, config, insights


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    # 全關基態
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "0")
    monkeypatch.setattr(config, "EXPERT_SKILLS", False)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_PARALLEL", False)
    monkeypatch.setattr(config, "NORMS_LOOP", False)
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.0)
    monkeypatch.setattr(config, "DEPLOY_VERIFY", False)
    monkeypatch.setattr(config, "CLARIFY_ASYNC", False)
    monkeypatch.setattr(config, "INTENT_LOOP", False)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    return tmp_path


def test_stage2_when_all_off():
    out = insights.stage_readiness()
    assert out["stage"] == "2" and out["canaries_on"] == 0
    assert len(out["canaries"]) == 8 and len(out["conditions"]) == 4
    assert all(not c["on"] for c in out["canaries"])


def test_stage3_progress_and_condition_measurement(monkeypatch):
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "1")
    monkeypatch.setattr(config, "EXPERT_SKILLS", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(
        insights,
        "trust_metrics",
        lambda days=7, state_dir=None: {
            "merged": 12,
            "zero_touch": 11,
            "zero_touch_rate": 0.917,
            "interventions": {"total": 1, "per_week": 1.0, "by_category": {"context_feeding": 1}},
            "events": {"task_failed": 2, "slo_brake": 0},
        },
    )
    out = insights.stage_readiness()
    assert out["stage"] == "3-progress" and out["canaries_on"] == 2
    conds = {c["key"]: c for c in out["conditions"]}
    assert conds["zero_touch"]["ok"] is True
    assert conds["interventions"]["ok"] is True, "零 output_review 且 ≤2/週"
    assert conds["paging"]["ok"] is True, "sinks 已設"
    assert conds["slo_armed"]["ok"] is False, "門檻=0 未武裝"


def test_conditions_green_but_no_streak_stays_progress(monkeypatch):
    for flag, val in [
        ("OBJECTIVE_GATE", "1"),
        ("EXPERT_SKILLS", True),
        ("AUTOPILOT_INVESTIGATION_PARALLEL", True),
        ("NORMS_LOOP", True),
        ("SLO_ZERO_TOUCH_MIN", 0.8),
        ("DEPLOY_VERIFY", True),
    ]:
        monkeypatch.setattr(config, flag, val)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(
        insights,
        "trust_metrics",
        lambda days=7, state_dir=None: {
            "merged": 20,
            "zero_touch": 19,
            "zero_touch_rate": 0.95,
            "interventions": {"total": 0, "per_week": 0.0, "by_category": {}},
            "events": {"slo_brake": 1},
        },
    )
    out = insights.stage_readiness()
    # D5 起 3-ready 另要求 streak>=14(歷史空=0)→ 快照全綠仍為 progress;
    # ready 路徑由 test_stage_streak.test_stage_ready_requires_streak 覆蓋。
    assert out["stage"] == "3-progress" and out["canaries_on"] == 6 and out["streak"] == 0


def test_zero_touch_needs_sample(monkeypatch):
    monkeypatch.setattr(
        insights,
        "trust_metrics",
        lambda days=7, state_dir=None: {
            "merged": 2,
            "zero_touch": 2,
            "zero_touch_rate": 1.0,
            "interventions": {"per_week": 0.0, "by_category": {}},
            "events": {},
        },
    )
    out = insights.stage_readiness()
    assert {c["key"]: c for c in out["conditions"]}["zero_touch"]["ok"] is False, "樣本<5 不算達標"
