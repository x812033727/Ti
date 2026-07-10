"""GET /api/autopilot 併入 autopilot 心跳（<state dir>/status.json）。

契約：檔案存在且合法 → 原樣併入 `heartbeat` 欄位（含巢狀 workers 子行程活性欄，
routes 不需特別處理即原樣 passthrough）；不存在或壞損 → null；既有欄位
（paused/counts/dryrun/repo）不受影響。
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
    # 近窗完成率欄位（排除 parked/pending）：無終局任務時 rate=None、total=0
    assert "completion" in data
    assert data["completion"]["rate"] is None and data["completion"]["total"] == 0


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


def test_autopilot_status_exposes_pr_budget(client, tmp_path):
    """每日 PR 預算（第五輪 F4）：used=UTC 當日 audit 內 pr 非空筆數、cap=config 值；
    無 audit 檔 used=0；壞行/非當日/pr 空皆不計。"""
    import time as _time

    now = _time.time()
    rows = [
        {"ts": now, "task_id": 1, "outcome": "merged", "pr": 11},
        {"ts": now, "task_id": 2, "outcome": "merge_pending", "pr": 12},
        {"ts": now, "task_id": 3, "outcome": "no_changes", "pr": None},  # 無 PR 不計
        {"ts": now - 2 * 86400, "task_id": 4, "outcome": "merged", "pr": 13},  # 非當日不計
    ]
    (tmp_path / "audit.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n壞行不是 json\n", encoding="utf-8"
    )
    data = client.get("/api/autopilot").json()
    assert data["pr_budget"] == {"used": 2, "cap": config.AUTOPILOT_DAILY_PR_BUDGET}


def test_autopilot_status_pr_budget_without_audit(client):
    data = client.get("/api/autopilot").json()
    assert data["pr_budget"]["used"] == 0


def test_autopilot_status_corrupt_heartbeat_is_null(client, tmp_path):
    (tmp_path / "status.json").write_text("{壞掉的 json", encoding="utf-8")
    data = client.get("/api/autopilot").json()
    assert data["heartbeat"] is None


def test_autopilot_status_exposes_workers(client, tmp_path):
    """巢狀 workers（子行程活性）原樣 passthrough——鎖住「routes 無需改動」契約。"""
    hb = {
        "state": "running",
        "task_id": 42,
        "sleep_until": None,
        "updated_at": 1000.0,
        "quota": {"claude": 12},
        "last_activity_at": 1783140425.0,
        "workers": {"count": 5, "cpu_active": True},
    }
    (tmp_path / "status.json").write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
    data = client.get("/api/autopilot").json()
    assert data["heartbeat"]["workers"] == {"count": 5, "cpu_active": True}


def test_autopilot_status_exposes_turn_fields(client, tmp_path):
    """current_expert / turn_started_at / last_activity_at 由 status.json 原樣進 heartbeat。"""
    hb = {
        "state": "running",
        "task_id": 42,
        "sleep_until": None,
        "updated_at": 1000.0,
        "quota": {"claude": 12},
        "last_activity_at": 1783140430.0,
        "workers": {"count": 5, "cpu_active": True},
        "current_expert": "engineer",
        "turn_started_at": 1783140400.0,
    }
    (tmp_path / "status.json").write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
    data = client.get("/api/autopilot").json()
    assert data["heartbeat"]["current_expert"] == "engineer"
    assert data["heartbeat"]["turn_started_at"] == 1783140400.0
    assert data["heartbeat"]["last_activity_at"] == 1783140430.0
