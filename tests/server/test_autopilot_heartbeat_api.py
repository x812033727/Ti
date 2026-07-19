"""GET /api/autopilot 併入 autopilot 心跳（<state dir>/status.json）。

契約：檔案存在且合法 → 原樣併入 `heartbeat` 欄位（含巢狀 workers 子行程活性欄，
routes 不需特別處理即原樣 passthrough）；不存在或壞損 → null；既有欄位
（paused/counts/dryrun/repo）不受影響。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio import config

ROOT = Path(__file__).resolve().parents[2]


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


@pytest.mark.parametrize("cpu_active", [True, False, None])
def test_autopilot_status_exposes_liveness_baseline_values(client, tmp_path, cpu_active):
    """#2 baseline：API heartbeat 必須帶出可直接判死的 last_activity_at / workers.cpu_active。

    特別鎖 False：不能被 truthy fallback 寫成 null/缺欄，否則 dead_task 黑樣本會失效。
    """
    hb = {
        "state": "running",
        "task_id": 42,
        "sleep_until": None,
        "updated_at": 1783140485.0,
        "last_activity_at": 1783140425.0,
        "workers": {"count": 5, "cpu_active": cpu_active},
    }
    (tmp_path / "status.json").write_text(json.dumps(hb), encoding="utf-8")

    heartbeat = client.get("/api/autopilot").json()["heartbeat"]

    assert type(heartbeat["last_activity_at"]) in {int, float}
    assert heartbeat["last_activity_at"] == 1783140425.0
    assert heartbeat["workers"]["cpu_active"] is cpu_active


def test_autopilot_status_old_heartbeat_missing_liveness_fields_is_passthrough(client, tmp_path):
    """舊 status.json 缺 last_activity_at/workers 時 API 仍可回 heartbeat，不得 500。"""
    hb = {
        "state": "running",
        "task_id": 42,
        "sleep_until": None,
        "updated_at": 1783140485.0,
    }
    (tmp_path / "status.json").write_text(json.dumps(hb), encoding="utf-8")

    data = client.get("/api/autopilot").json()

    assert data["heartbeat"] == hb


def test_monitoring_doc_records_heartbeat_threshold_rationale():
    """#2 baseline：文件要寫清楚 60s 心跳推得生產門檻至少 300s。"""
    text = (ROOT / "docs/guides/autopilot-monitoring.md").read_text(encoding="utf-8")

    assert "_HEARTBEAT_INTERVAL_S = 60.0" in (ROOT / "studio/autopilot.py").read_text(
        encoding="utf-8"
    )
    assert re.search(r"60s|60 秒|每 ~60s", text)
    assert re.search(
        r"≥\s*300s|>=\s*300s|至少\s*300\s*秒", text
    ), "文件需明確記錄門檻選值依據：心跳 60s，生產 stale threshold 應 ≥300s"


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
