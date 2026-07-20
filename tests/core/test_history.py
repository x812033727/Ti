"""測試 session 歷史存檔/讀取（純檔案 IO，不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import config, history


@pytest.fixture(autouse=True)
def _tmp_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")


def _ev(type_, **payload):
    return {"type": type_, "session_id": "s1", "ts": 0, "payload": payload}


def test_start_record_load():
    history.start_session("s1", "做一個 BMI CLI")
    history.record_event("s1", _ev("phase_change", phase="拆解"))
    history.record_event("s1", _ev("expert_message", text="hi"))
    events = history.load_events("s1")
    assert [e["type"] for e in events] == ["phase_change", "expert_message"]
    meta = history.get_meta("s1")
    assert meta["requirement"] == "做一個 BMI CLI"
    assert meta["status"] == "running"


def test_finish_derives_completed():
    history.start_session("s1", "需求")
    history.record_event("s1", _ev("done", completed=True))
    meta = history.finish_session("s1")
    assert meta["status"] == "completed"
    assert meta["n_events"] == 1


def test_finish_derives_stopped_and_error():
    history.start_session("a", "x")
    history.record_event("a", _ev("done", completed=False, stopped=True))
    assert history.finish_session("a")["status"] == "stopped"

    history.start_session("b", "y")
    history.record_event("b", _ev("done", completed=True))
    history.record_event("b", _ev("error", message="boom"))
    assert history.finish_session("b")["status"] == "error"


def test_list_sessions_sorted_newest_first():
    m1 = history.start_session("old", "a")
    m1["started_at"] = 100
    history._write_meta("old", m1)
    m2 = history.start_session("new", "b")
    m2["started_at"] = 200
    history._write_meta("new", m2)

    sessions = history.list_sessions()
    assert [s["session_id"] for s in sessions] == ["new", "old"]


def test_missing_session():
    assert history.get_meta("nope") is None
    assert history.load_events("nope") == []
    assert history.finish_session("nope") is None


# --- iter_events：惰性疊代（load_events 的底層，計數路徑 O(1) 記憶體）------


def test_iter_events_matches_load_events():
    history.start_session("s1", "req")
    for i in range(5):
        history.record_event("s1", _ev("phase_change", phase=f"p{i}"))
    assert list(history.iter_events("s1")) == history.load_events("s1")


def test_iter_events_skips_blank_and_corrupt_lines():
    history.start_session("s1", "req")
    history.record_event("s1", _ev("phase_change", phase="a"))
    with history._events_path("s1").open("a", encoding="utf-8") as f:
        f.write("\n{ 不是 JSON\n")
    history.record_event("s1", _ev("done", completed=True))
    types = [e["type"] for e in history.iter_events("s1")]
    assert types == ["phase_change", "done"]  # 空行/壞行跳過、順序不變


def test_iter_events_missing_session_yields_nothing():
    assert list(history.iter_events("nosuch")) == []


def test_mark_interrupted_counts_via_iterator():
    history.start_session("s1", "req")
    for i in range(3):
        history.record_event("s1", _ev("phase_change", phase=f"p{i}"))
    assert history.mark_interrupted("s1", "test")
    assert history.get_meta("s1")["n_events"] == 3
