"""測試 /api/metrics 運維可視化端點：內容正確、且受 require_auth 保護。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, history, ws


@pytest.fixture
def client():
    from studio.server import app

    return TestClient(app)


def test_metrics_reports_sessions_history_workspaces(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → require_auth 放行
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 5)
    monkeypatch.setattr(config, "HISTORY_MAX_COUNT", 200)
    monkeypatch.setattr(config, "HISTORY_MAX_AGE", 0)
    monkeypatch.setattr(ws, "_active_sessions", 2)
    # 造兩個 history session（completed + running）與兩個 workspace 目錄。
    m = history.start_session("s1", "需求1")
    m["status"] = "completed"
    history._write_meta("s1", m)
    history.start_session("s2", "需求2")  # 維持 running
    (config.WORKSPACE_ROOT / "s1").mkdir(parents=True)
    (config.WORKSPACE_ROOT / "s2").mkdir()

    data = client.get("/api/metrics").json()
    assert data["sessions"] == {"active": 2, "max_concurrent": 5}
    assert data["history"]["total"] == 2
    assert data["history"]["by_status"] == {"completed": 1, "running": 1}
    assert data["history"]["retention"] == {"max_count": 200, "max_age_s": 0}
    assert data["workspaces"]["count"] == 2


def test_metrics_empty_state(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(ws, "_active_sessions", 0)
    data = client.get("/api/metrics").json()
    assert data["sessions"]["active"] == 0
    assert data["history"]["total"] == 0
    assert data["history"]["by_status"] == {}
    assert data["workspaces"]["count"] == 0


def test_metrics_requires_auth(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")  # 門禁啟用、未登入
    assert client.get("/api/metrics").status_code == 401


def test_metrics_aggregates_parallel(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", 3)
    # 兩個曾並行的 session + 一個未並行（enabled=False，不計入聚合）。
    for sid, par in [
        (
            "p1",
            {
                "enabled": True,
                "waves": 2,
                "lanes_max": 3,
                "merge_conflicts": 1,
                "lane_exceptions": 1,
                "deferred": 2,
                "conflict_retries": 1,
                "wall_clock_s": 4.0,
                "serial_estimate_s": 9.0,
                "speedup": 2.25,
            },
        ),
        (
            "p2",
            {
                "enabled": True,
                "waves": 1,
                "lanes_max": 2,
                "merge_conflicts": 0,
                "lane_exceptions": 0,
                "deferred": 1,
                "conflict_retries": 0,
                "wall_clock_s": 2.0,
                "serial_estimate_s": 3.0,
                "speedup": 1.5,
            },
        ),
        ("s1", {"enabled": False}),
    ]:
        m = history.start_session(sid, "需求")
        m["parallel"] = par
        history._write_meta(sid, m)

    pa = client.get("/api/metrics").json()["parallel"]
    assert pa["enabled_runs"] == 2  # s1 的 enabled=False 不計
    assert pa["peak_lanes"] == 3
    assert pa["total_waves"] == 3
    assert pa["merge_conflicts"] == 1
    assert pa["lane_exceptions"] == 1  # 跨 session 聚合：1 + 0
    assert pa["deferred"] == 3  # 2 + 1
    assert pa["conflict_retries"] == 1  # 1 + 0
    assert pa["avg_speedup"] == 1.88  # (2.25 + 1.5) / 2，四捨五入
    assert pa["wall_clock_saved_s"] == 6.0  # (9-4) + (3-2)
    assert pa["config"] == {"enabled": True, "lanes": 3}


def test_metrics_parallel_empty(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    pa = client.get("/api/metrics").json()["parallel"]
    assert pa["enabled_runs"] == 0
    assert pa["config"]["enabled"] is False
