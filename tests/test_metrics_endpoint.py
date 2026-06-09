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
