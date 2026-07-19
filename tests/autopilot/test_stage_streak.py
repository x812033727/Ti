"""宣告 streak(軌 D5):每日快照冪等/連續天數計數/斷檔中斷/今日未快照不中斷。"""

from __future__ import annotations

import time

import pytest

from studio import backlog, config, insights


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    return tmp_path


def _seed_history(days_ok):
    """days_ok: {偏移天數(0=今天): all_ok bool};直接落 jsonl。"""
    from studio import jsonl_log

    now = time.time()
    for off, ok in days_ok.items():
        ts = now - off * 86400
        jsonl_log.append(
            insights._stage_history_path(),
            {
                "ts": ts,
                "day": insights._utc_day(ts),
                "all_ok": ok,
                "conditions": {},
                "canaries_on": 1,
            },
        )


def test_record_snapshot_idempotent_per_day():
    assert insights.record_stage_snapshot() is True
    assert insights.record_stage_snapshot() is False, "同日第二次=冪等跳過"
    from studio import jsonl_log

    assert len(jsonl_log.read_window(insights._stage_history_path(), 2)) == 1


def test_streak_counts_consecutive_green():
    _seed_history({0: True, 1: True, 2: True, 3: False, 4: True})
    assert insights.stage_streak() == 3, "遇 False 即斷"


def test_streak_today_missing_does_not_break():
    _seed_history({1: True, 2: True})
    assert insights.stage_streak() == 2, "今日尚未快照(排程未跑)不中斷,從昨日起算"


def test_streak_gap_breaks():
    _seed_history({0: True, 2: True})  # 缺第 1 天
    assert insights.stage_streak() == 1, "斷檔=中斷"


def test_stage_ready_requires_streak(monkeypatch):
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
    assert out["stage"] == "3-progress" and out["streak"] == 0, "快照全綠但 streak<14=仍 progress"
    _seed_history({i: True for i in range(14)})
    out = insights.stage_readiness()
    assert out["streak"] >= 14 and out["stage"] == "3-ready"
