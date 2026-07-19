"""讀取熱路徑快取(效能強化 B3):backlog/lessons mtime 快取 + overview + ws 串流補放。

守護不變量:
- backlog._load 唯讀路徑快取命中(同 stat 訊號不重讀);外部程序改檔(mtime/size 變)後失效;
  寫路徑(set_status 等)mutable=True 繞過快取,寫後讀立即看到新值(寫後一致性)。
- overview() 與 counts()+completion_stats() 逐字段等價(等價 oracle)。
- lessons 同款快取;add_many 後 recent 立即可見。
- /api/history/{sid}/events 預設全量(向後相容);offset/limit 切片正確。
"""

from __future__ import annotations

import json

import pytest

from studio import backlog, config, lessons


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(backlog, "_read_cache", {})
    monkeypatch.setattr(lessons, "_read_cache", {})
    return tmp_path


# --- backlog 快取 -------------------------------------------------------------


def test_cache_hit_skips_reparse(monkeypatch):
    t = backlog.add("任務一")
    assert t
    reads = {"n": 0}
    orig = json.loads

    def counting_loads(s, *a, **k):
        reads["n"] += 1
        return orig(s, *a, **k)

    monkeypatch.setattr(backlog.json, "loads", counting_loads)
    backlog.list_tasks()
    first = reads["n"]
    backlog.counts()
    backlog.completion_stats()
    backlog.next_pending()
    assert reads["n"] == first, "同一份檔案未變,唯讀路徑不得重複 parse"


def test_write_then_read_sees_fresh_value():
    t = backlog.add("任務一")
    backlog.list_tasks()  # 灌快取
    backlog.set_status(t["id"], "done")
    assert backlog.list_tasks("done")[0]["id"] == t["id"], "寫後一致性:_save 後快取須刷新"
    assert backlog.counts()["done"] == 1


def test_external_file_change_invalidates(tmp_path):
    backlog.add("任務一")
    backlog.list_tasks()  # 灌快取
    # 模擬「另一個程序」直接改檔(繞過本程序的 _save 刷新)
    p = backlog._path(None)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["tasks"][0]["title"] = "被外部程序改掉"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert backlog.list_tasks()[0]["title"] == "被外部程序改掉", "stat 訊號變了必須失效重讀"


def test_overview_equivalence_oracle():
    for i, status in enumerate(["done", "failed", "parked", "pending", "done"]):
        t = backlog.add(f"任務{i}")
        if status != "pending":
            backlog.set_status(t["id"], status)
    ov = backlog.overview(window=50)
    assert ov["counts"] == backlog.counts(), "overview.counts 必須與 counts() 逐字段等價"
    assert ov["completion"] == backlog.completion_stats(
        window=50
    ), "overview.completion 必須與 completion_stats() 逐字段等價"


# --- lessons 快取 -------------------------------------------------------------


def test_lessons_cache_and_write_consistency(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(lessons, "_path", lambda: tmp_path / "lessons.json")
    n = lessons.add_many(["教訓一:先寫測試"], session_id="s1", requirement="r")
    assert n == 1
    assert any("教訓一" in it["text"] for it in lessons.all_lessons())
    reads = {"n": 0}
    orig = json.loads

    def counting_loads(s, *a, **k):
        reads["n"] += 1
        return orig(s, *a, **k)

    monkeypatch.setattr(lessons.json, "loads", counting_loads)
    lessons.all_lessons()
    first = reads["n"]
    lessons.all_lessons()
    assert reads["n"] == first, "lessons 唯讀路徑快取命中"
    lessons.add_many(["教訓二:後寫文件"], session_id="s1", requirement="r")
    assert any("教訓二" in it["text"] for it in lessons.all_lessons()), "寫後一致性"


# --- /api/history events offset/limit ----------------------------------------


@pytest.mark.asyncio
async def test_history_events_default_full_and_sliced(tmp_path, monkeypatch):
    from studio import history, routes

    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    sid = "sess-1"
    history.start_session(sid, "需求")
    for i in range(5):
        history.record_event(sid, {"type": "expert_message", "payload": {"i": i}})

    full = await routes.history_events(sid)
    events = json.loads(full.body)["events"]
    assert len(events) == 5, "預設全量(前端重播依賴)"

    sliced = await routes.history_events(sid, offset=2, limit=2)
    ev = json.loads(sliced.body)["events"]
    assert [e["payload"]["i"] for e in ev] == [2, 3]

    beyond = await routes.history_events(sid, offset=99)
    assert json.loads(beyond.body)["events"] == []
