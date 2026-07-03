"""GET /api/autopilot 併入 autopilot 心跳（<state dir>/status.json）。

契約：檔案存在且合法 → 原樣併入 `heartbeat` 欄位；不存在或壞損 → null；
既有欄位（paused/counts/dryrun/repo）不受影響。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from studio import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → require_auth 放行
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)  # backlog/心跳都指向 tmp
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "AUTOPILOT_PAUSED")
    from studio.server import app

    return TestClient(app)


def test_autopilot_status_without_heartbeat_is_null(client):
    data = client.get("/api/autopilot").json()
    assert data["heartbeat"] is None
    # 既有欄位維持不變
    assert data["paused"] is False
    assert "counts" in data and "dryrun" in data and "repo" in data


def test_autopilot_status_includes_heartbeat(client, tmp_path):
    hb = {
        "state": "quota_sleep",
        "task_id": None,
        "sleep_until": 1234.5,
        "updated_at": 1000.0,
        "quota": {"claude": 95, "codex": None},
    }
    (tmp_path / "status.json").write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
    data = client.get("/api/autopilot").json()
    assert data["heartbeat"] == hb


def test_autopilot_status_corrupt_heartbeat_is_null(client, tmp_path):
    (tmp_path / "status.json").write_text("{壞掉的 json", encoding="utf-8")
    data = client.get("/api/autopilot").json()
    assert data["heartbeat"] is None
