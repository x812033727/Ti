"""SLO 自動煞車(第 3 階 A4):信任低於門檻→自產日額砍半+每日一次推播。

守護不變量:
- 預設(TI_SLO_ZERO_TOUCH_MIN=0)完全 no-op:因子恆 1、零指標讀取、零通知。
- 生效條件三閘缺一不可:門檻>0、樣本 ≥ SLO_MIN_MERGED、rate < 門檻。
- 煞車=cap//2(地板 1);推播 slo_brake 每 UTC 日至多一次。
- 指標讀取拋錯不煞車(煞車是加值,不得影響主迴圈)。
"""

from __future__ import annotations

import pytest

from studio import autopilot, config


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DISCOVERED_DAILY_CAP", 20)
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.0)
    monkeypatch.setattr(config, "SLO_MIN_MERGED", 5)
    monkeypatch.setattr(autopilot, "_slo_brake_notified_day", None)
    monkeypatch.setattr(autopilot, "_discovered_added_today", lambda now=None: 0)


def _metrics(monkeypatch, rate, merged=10):
    calls = {"n": 0}

    def fake(days=7):
        calls["n"] += 1
        return {"zero_touch_rate": rate, "merged": merged}

    monkeypatch.setattr(autopilot.insights, "trust_metrics", fake)
    return calls


def _notify_spy(monkeypatch):
    sent = []
    monkeypatch.setattr(
        autopilot.notify, "send_bg", lambda kind, title, **kw: sent.append((kind, kw))
    )
    return sent


def test_disabled_by_default_zero_cost(monkeypatch):
    calls = _metrics(monkeypatch, rate=0.1)
    sent = _notify_spy(monkeypatch)
    assert autopilot._discovered_budget_left("測試", 20) == 20
    assert calls["n"] == 0, "門檻=0 時連指標都不讀(零成本 no-op)"
    assert not sent


def test_brake_halves_cap_and_notifies_once_per_day(monkeypatch):
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.8)
    _metrics(monkeypatch, rate=0.5, merged=10)
    sent = _notify_spy(monkeypatch)
    assert autopilot._discovered_budget_left("測試", 20) == 10, "cap 20→10(砍半)"
    assert autopilot._discovered_budget_left("測試", 20) == 10
    assert [k for k, _ in sent] == ["slo_brake"], "同日只推播一次"
    assert sent[0][1]["rate"] == 0.5 and sent[0][1]["threshold"] == 0.8


def test_no_brake_when_sample_too_small(monkeypatch):
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.8)
    _metrics(monkeypatch, rate=0.0, merged=4)  # < SLO_MIN_MERGED=5
    sent = _notify_spy(monkeypatch)
    assert autopilot._discovered_budget_left("測試", 20) == 20, "樣本不足不煞車(冷啟動)"
    assert not sent


def test_no_brake_when_rate_meets_threshold(monkeypatch):
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.8)
    _metrics(monkeypatch, rate=0.9)
    sent = _notify_spy(monkeypatch)
    assert autopilot._discovered_budget_left("測試", 20) == 20
    assert not sent


def test_metrics_failure_never_brakes(monkeypatch):
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.8)

    def boom(days=7):
        raise OSError("audit unreadable")

    monkeypatch.setattr(autopilot.insights, "trust_metrics", boom)
    sent = _notify_spy(monkeypatch)
    assert autopilot._discovered_budget_left("測試", 20) == 20, "指標壞了不擋自產"
    assert not sent


def test_rate_none_no_brake(monkeypatch):
    monkeypatch.setattr(config, "SLO_ZERO_TOUCH_MIN", 0.8)
    _metrics(monkeypatch, rate=None, merged=10)
    assert autopilot._discovered_budget_left("測試", 20) == 20
