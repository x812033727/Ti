"""例外收件匣(軌 F1):澄清票/停放/page 級事件聚合。"""

from __future__ import annotations

import pytest

from studio import backlog, config, insights, jsonl_log


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def test_empty_state():
    out = insights.attention()
    assert out == {"clarify": [], "parked": [], "events": [], "pending_clarify": 0}


def test_clarify_vs_plain_parked_split():
    t1 = backlog.add("歧義任務", "", source="manual")
    t2 = backlog.add("普通停放", "", source="eval")
    backlog.set_status(t1["id"], "parked", note="[待澄清] 要哪個環境?", clarify="要哪個環境?")
    backlog.set_status(t2["id"], "parked", note="等外部依賴")
    out = insights.attention()
    assert out["pending_clarify"] == 1
    assert [r["id"] for r in out["clarify"]] == [t1["id"]]
    assert out["clarify"][0]["clarify"] == "要哪個環境?"
    assert [r["id"] for r in out["parked"]] == [t2["id"]]
    assert out["parked"][0]["note"] == "等外部依賴"


def test_events_page_only_and_noise_excluded():
    path = config.AUTOPILOT_STATE_DIR / "events.jsonl"
    jsonl_log.append(path, {"kind": "task_failed", "title": "任務失敗", "task_id": 9})
    jsonl_log.append(path, {"kind": "critic_reject", "title": "digest 級不進收件匣"})
    jsonl_log.append(path, {"kind": "test", "title": "自證雜訊排除"})
    jsonl_log.append(path, {"kind": "daily_digest", "title": "日報排除"})
    out = insights.attention()
    assert [e["kind"] for e in out["events"]] == ["task_failed"]
    assert out["events"][0]["task_id"] == 9


def test_days_clamped_and_sorted_desc(monkeypatch):
    import time

    now = time.time()
    path = config.AUTOPILOT_STATE_DIR / "events.jsonl"
    jsonl_log.append(path, {"kind": "task_failed", "title": "舊", "ts": now - 100})
    jsonl_log.append(path, {"kind": "loop_stall", "title": "新", "ts": now - 50})
    seen = {}
    real = insights.notify.read_events

    def spy(days, *, state_dir=None):
        seen["days"] = days
        return real(days, state_dir=state_dir)

    monkeypatch.setattr(insights.notify, "read_events", spy)
    out = insights.attention(days=999)
    assert seen["days"] == 30, "days 夾 1..30"
    assert [e["kind"] for e in out["events"]] == ["loop_stall", "task_failed"]
