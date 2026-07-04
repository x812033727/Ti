"""測試幽靈 running meta 掃除（history.sweep_stale_running）：服務／autopilot 被 restart
殺掉時 finish_session 沒跑到，meta 永遠停在 running（網站無限顯示 ⏳ 執行中）。掃除規則：
status==running 且 sid 不在 active_sids 且最後活動（events 檔 mtime）超過 stale_after_s 秒
→ mark_interrupted 標 error。純檔案 IO，不需 LLM。"""

from __future__ import annotations

import os
import time

import pytest

from studio import config, history


@pytest.fixture(autouse=True)
def _tmp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")


def _make(sid, *, status="running", age_s=0.0):
    """建一個 session（meta+events），把 events 檔 mtime 設成 age_s 秒前當最後活動時間。"""
    meta = history.start_session(sid, f"req-{sid}")
    meta["status"] = status
    history._write_meta(sid, meta)
    t = time.time() - age_s
    os.utime(history._events_path(sid), (t, t))
    return meta


def test_stale_running_swept_to_error_with_note():
    _make("ghost", age_s=7200)
    swept = history.sweep_stale_running(stale_after_s=3600)
    assert swept == ["ghost"]
    meta = history.get_meta("ghost")
    assert meta["status"] == "error"
    assert "stale-running" in meta.get("note", "")
    assert meta.get("finished_at"), "掃除後應補 finished_at，供保留策略回收"


def test_fresh_running_not_swept():
    """最後活動仍在門檻內的 running 是活場，不得掃。"""
    _make("live", age_s=60)
    assert history.sweep_stale_running(stale_after_s=3600) == []
    assert history.get_meta("live")["status"] == "running"


def test_active_sids_protected_even_if_stale():
    """active_sids（如 autopilot 正在跑的 sid）即使久無事件也絕不掃——雙保險。"""
    _make("inflight", age_s=7200)
    swept = history.sweep_stale_running(active_sids=frozenset({"inflight"}), stale_after_s=3600)
    assert swept == []
    assert history.get_meta("inflight")["status"] == "running"


@pytest.mark.parametrize("status", ["completed", "incomplete", "error", "stopped"])
def test_non_running_untouched(status):
    _make(f"s-{status}", status=status, age_s=7200)
    assert history.sweep_stale_running(stale_after_s=3600) == []
    assert history.get_meta(f"s-{status}")["status"] == status


def test_no_events_file_falls_back_to_meta_started_at():
    """events 檔不存在時退回 meta 時間戳判 stale（老 started_at → 掃）。"""
    meta = history.start_session("no-events", "req")
    meta["started_at"] = time.time() - 7200
    history._write_meta("no-events", meta)
    history._events_path("no-events").unlink()
    assert history.sweep_stale_running(stale_after_s=3600) == ["no-events"]
    assert history.get_meta("no-events")["status"] == "error"


def test_mixed_batch_only_ghosts_swept():
    _make("ghost1", age_s=7200)
    _make("ghost2", age_s=9999)
    _make("live", age_s=10)
    _make("done", status="completed", age_s=7200)
    swept = history.sweep_stale_running(stale_after_s=3600)
    assert sorted(swept) == ["ghost1", "ghost2"]
    assert history.get_meta("live")["status"] == "running"
    assert history.get_meta("done")["status"] == "completed"


def test_sweep_emits_summary_log(caplog):
    _make("ghost", age_s=7200)
    with caplog.at_level("INFO", logger="ti.history"):
        history.sweep_stale_running(stale_after_s=3600)
    assert "stale-running 掃除" in caplog.text


def test_lifespan_startup_sweeps_ghosts(monkeypatch):
    """ti.service 重啟（lifespan startup）也會治癒幽靈 running——autopilot 掛掉時的兜底。"""
    from fastapi.testclient import TestClient

    from studio.server import app

    _make("ghost", age_s=7200)
    _make("live", age_s=10)
    with TestClient(app):  # 進入 = 觸發 lifespan startup
        pass
    assert history.get_meta("ghost")["status"] == "error"
    assert history.get_meta("live")["status"] == "running", "busy 的活場不得被啟動掃除誤殺"
