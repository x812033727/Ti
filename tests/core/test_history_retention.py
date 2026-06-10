"""測試歷史 / 工作區自動回收（GC）：enforce_retention 的數量/年齡規則、running 守門、
workspace 一併刪除，以及 finish_session 觸發與手動端點。純檔案 IO，不需 LLM。"""

from __future__ import annotations

import os
import time

import pytest

from studio import config, history, workspace


@pytest.fixture(autouse=True)
def _tmp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")


def _make(sid, *, status="completed", started_at=0.0, age_s=None, with_workspace=False):
    """建一個 session：meta（指定 status/started_at）+ events 檔；可選造 workspace 與活動年齡。"""
    meta = history.start_session(sid, f"req-{sid}")
    meta["status"] = status
    meta["started_at"] = started_at
    history._write_meta(sid, meta)
    if age_s is not None:
        # 把 events 檔 mtime 設成 age_s 秒前，讓 _last_activity_ts 視為該活動時間。
        t = time.time() - age_s
        os.utime(history._events_path(sid), (t, t))
    if with_workspace:
        ws = workspace.workspace_path(sid)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "out.txt").write_text("x", encoding="utf-8")
    return meta


def _ids():
    return {m["session_id"] for m in history.list_sessions()}


def test_count_cap_keeps_newest_deletes_surplus():
    for i in range(5):
        _make(f"s{i}", started_at=float(i))  # s4 最新、s0 最舊
    deleted = history.enforce_retention(max_count=3, max_age_s=0)
    assert deleted == 2
    assert _ids() == {"s4", "s3", "s2"}


def test_age_cap_deletes_old_keeps_recent():
    _make("old", age_s=10_000)
    _make("fresh", age_s=1)
    deleted = history.enforce_retention(max_count=0, max_age_s=3600)
    assert deleted == 1
    assert _ids() == {"fresh"}


def test_running_never_deleted():
    # 又老又超量的 running 場仍須保留（delete_session 守門擋下）。
    _make("run", status="running", started_at=0.0, age_s=10_000)
    for i in range(3):
        _make(f"done{i}", started_at=float(i + 1))
    deleted = history.enforce_retention(max_count=1, max_age_s=3600)
    assert deleted == 2  # 只刪 done0 / done1，done2 與 run 留下
    assert _ids() == {"run", "done2"}


def test_disabled_is_noop():
    for i in range(5):
        _make(f"s{i}", started_at=float(i), age_s=10_000)
    assert history.enforce_retention(max_count=0, max_age_s=0) == 0
    assert len(history.list_sessions()) == 5


def test_removes_workspace_dir():
    _make("keep", started_at=2.0, with_workspace=True)
    _make("drop", started_at=1.0, with_workspace=True)
    drop_ws = workspace.workspace_path("drop")
    keep_ws = workspace.workspace_path("keep")
    assert drop_ws.exists()
    history.enforce_retention(max_count=1, max_age_s=0)
    assert not drop_ws.exists()  # workspace 隨 session 一併回收
    assert keep_ws.exists()


def test_removes_orphan_lanes_dir():
    """回收時連並行支線的 .lanes 兄弟目錄一併清掉（兜底程序中途崩潰未收尾的殘留）。"""
    _make("drop", started_at=1.0, with_workspace=True)
    ws = workspace.workspace_path("drop")
    lanes = ws.parent / f"{ws.name}.lanes" / "task-1"
    lanes.mkdir(parents=True, exist_ok=True)
    (lanes / "leftover.txt").write_text("x", encoding="utf-8")

    assert history.delete_session("drop") is True
    assert not ws.exists()
    assert not (ws.parent / f"{ws.name}.lanes").exists(), ".lanes 殘留未被回收"


def test_finish_session_triggers_retention(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_MAX_COUNT", 1)
    monkeypatch.setattr(config, "HISTORY_MAX_AGE", 0)
    _make("old", status="completed", started_at=0.0)  # 先放一個已結束的舊場
    # 新場跑完 → finish_session 觸發回收：舊場被清、剛 finish 的最新場保留、回傳 meta 有效。
    history.start_session("new", "需求")
    history.record_event(
        "new", {"type": "done", "session_id": "new", "ts": 0, "payload": {"completed": True}}
    )
    meta = history.finish_session("new")
    assert meta is not None and meta["status"] == "completed"
    assert _ids() == {"new"}


def test_retention_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from studio.server import app

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → require_auth 放行
    monkeypatch.setattr(config, "HISTORY_MAX_COUNT", 2)
    monkeypatch.setattr(config, "HISTORY_MAX_AGE", 0)
    for i in range(4):
        _make(f"s{i}", started_at=float(i))
    client = TestClient(app)
    resp = client.post("/api/history/cleanup/retention")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}
    assert _ids() == {"s3", "s2"}


def test_startup_sweep_reclaims_via_lifespan(monkeypatch):
    """以 `with TestClient(app)` 觸發 lifespan startup，驗證啟動時掃一次保留策略。"""
    from fastapi.testclient import TestClient

    from studio.server import app

    monkeypatch.setattr(config, "HISTORY_MAX_COUNT", 2)
    monkeypatch.setattr(config, "HISTORY_MAX_AGE", 0)
    for i in range(5):
        _make(f"s{i}", started_at=float(i))
    assert len(history.list_sessions()) == 5
    with TestClient(app):  # 進入 = 觸發 lifespan startup → 啟動掃描
        pass
    assert _ids() == {"s4", "s3"}


def test_reclaim_emits_log(caplog):
    for i in range(4):
        _make(f"s{i}", started_at=float(i))
    with caplog.at_level("INFO", logger="ti.history"):
        history.enforce_retention(max_count=2, max_age_s=0)
    assert "保留策略回收" in caplog.text
