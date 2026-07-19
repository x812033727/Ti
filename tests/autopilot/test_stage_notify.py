"""升階轉換推播(軌 G1):stage 變化/streak 里程碑推播、無變化不推、失敗不擋快照。"""

from __future__ import annotations

import time

import pytest

from studio import backlog, config, insights, jsonl_log, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


@pytest.fixture()
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_bg", lambda kind, title, **extra: calls.append((kind, title)))
    return calls


def _seed(off_days, stage, all_ok=True):
    ts = time.time() - off_days * 86400
    jsonl_log.append(
        insights._stage_history_path(),
        {
            "ts": ts,
            "day": insights._utc_day(ts),
            "stage": stage,
            "all_ok": all_ok,
            "conditions": {},
            "canaries_on": 8,
        },
    )


def _fake_readiness(stage, all_ok=True, monkeypatch=None):
    snap = {
        "stage": stage,
        "canaries_on": 8,
        "conditions": [{"key": "k", "ok": all_ok}],
    }
    monkeypatch.setattr(insights, "stage_readiness", lambda state_dir=None: snap)


def test_stage_change_pushes(monkeypatch, sent):
    _seed(1, "3-progress")
    _fake_readiness("3-ready", monkeypatch=monkeypatch)
    assert insights.record_stage_snapshot() is True
    assert len(sent) == 1 and sent[0][0] == "stage_changed"
    assert "3-progress → 3-ready" in sent[0][1]


def test_no_change_no_push_and_idempotent(monkeypatch, sent):
    _seed(1, "3-progress", all_ok=False)
    _fake_readiness("3-progress", all_ok=False, monkeypatch=monkeypatch)
    assert insights.record_stage_snapshot() is True
    assert sent == []
    assert insights.record_stage_snapshot() is False, "當日冪等"
    assert sent == []


def test_streak_milestone_pushes(monkeypatch, sent):
    for off in range(1, 7):
        _seed(off, "3-progress", all_ok=True)
    _fake_readiness("3-progress", all_ok=True, monkeypatch=monkeypatch)
    assert insights.record_stage_snapshot() is True
    assert len(sent) == 1 and "連續 7 天" in sent[0][1]


def test_push_failure_does_not_block_snapshot(monkeypatch):
    _seed(1, "2")
    _fake_readiness("3-progress", monkeypatch=monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("sink down")

    monkeypatch.setattr(notify, "send_bg", boom)
    assert insights.record_stage_snapshot() is True, "推播炸了快照照寫"


def test_first_snapshot_no_prev_no_push(monkeypatch, sent):
    _fake_readiness("3-progress", all_ok=False, monkeypatch=monkeypatch)
    assert insights.record_stage_snapshot() is True
    assert sent == [], "首筆無前值不推(避免部署即誤報)"
