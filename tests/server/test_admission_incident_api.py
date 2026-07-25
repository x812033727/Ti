"""Admission incident 的 public health 與 authenticated operator 投影。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio import admission_incidents, admission_mode, config


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "AUTOPILOT_PAUSED")
    admission_mode.bootstrap_at_task_boundary(
        "enforce",
        initial_effective="enforce",
        release_holds=lambda _mode: 0,
        state_dir=tmp_path,
    )
    from studio.server import app

    return TestClient(app)


def test_public_health_degrades_without_disclosing_incident_details(client, tmp_path: Path):
    admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 7),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )

    response = client.get("/api/health")
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["status"] == "degraded"
    assert body["intake_available"] is False
    assert body["error_code"] == "invalid_json"
    encoded = response.text
    assert "incident_id" not in encoded
    assert "first_seen_at" not in encoded
    assert str(tmp_path) not in encoded


def test_operator_api_exposes_safe_incident_timeline_and_recovery(client, tmp_path: Path):
    opened = admission_incidents.observe(
        admission_incidents.Faulted("state_unreadable", "shadow", 4),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )

    incident = client.get("/api/autopilot").json()["task_admission_incident"]

    assert incident["active"] is True
    assert incident["incident_id"] == opened.incident_id
    assert incident["error_code"] == "state_unreadable"
    assert incident["first_seen_at"] <= incident["last_seen_at"]
    assert incident["duration_s"] >= 0
    assert incident["last_effective_mode"] == "shadow"
    assert incident["last_effective_generation"] == 4
    assert str(tmp_path) not in str(incident)

    admission_incidents.observe(
        admission_incidents.IntakeRestored("enforce", 5),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    health = client.get("/api/health").json()
    recovered = client.get("/api/autopilot").json()["task_admission_incident"]

    assert health["status"] == "ok"
    assert health["intake_available"] is True
    assert health["error_code"] == ""
    assert recovered["active"] is False
    assert recovered["incident_id"] == opened.incident_id
    assert recovered["recovered_at"] is not None


def test_health_uses_worker_heartbeat_when_incident_ledger_is_unavailable(
    client,
    tmp_path: Path,
):
    (tmp_path / "admission_incident.json").write_text("{broken", encoding="utf-8")
    incident_id = "admission-" + "a" * 32
    heartbeat_incident = {
        "sequence": 1,
        "active": True,
        "incident_id": incident_id,
        "error_code": "ledger_write_failed",
        "first_seen_at": 100.0,
        "last_seen_at": 101.0,
        "duration_s": 1.0,
        "last_effective_mode": "enforce",
        "last_effective_generation": 7,
        "recovered_at": None,
        "durability": "memory_only",
        "diagnostic": "ledger_write_failed",
    }
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "state": "admission_mode_fault",
                "updated_at": 101.0,
                "admission_incident": heartbeat_incident,
            }
        ),
        encoding="utf-8",
    )

    health = client.get("/api/health").json()
    operator = client.get("/api/autopilot").json()["task_admission_incident"]

    assert health["status"] == "degraded"
    assert health["error_code"] == "ledger_write_failed"
    assert "incident_id" not in health
    assert operator == heartbeat_incident


def test_newer_memory_error_code_heartbeat_wins_over_same_durable_incident(
    client,
    tmp_path: Path,
    monkeypatch,
):
    opened = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 7),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    changed = admission_incidents.observe(
        admission_incidents.Faulted("state_unreadable", "enforce", 7),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    heartbeat_incident = admission_incidents.snapshot(state_dir=tmp_path)
    assert changed.incident_id == opened.incident_id
    assert heartbeat_incident["durability"] == "memory_only"
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "state": "admission_mode_fault",
                "updated_at": heartbeat_incident["last_seen_at"],
                "admission_incident": heartbeat_incident,
            }
        ),
        encoding="utf-8",
    )
    importlib.reload(admission_incidents)

    health = client.get("/api/health").json()
    operator = client.get("/api/autopilot").json()["task_admission_incident"]

    assert health["status"] == "degraded"
    assert health["error_code"] == "state_unreadable"
    assert operator["error_code"] == "state_unreadable"
    assert operator["sequence"] == heartbeat_incident["sequence"]


def test_newer_memory_recovery_heartbeat_wins_over_same_durable_incident(
    client,
    tmp_path: Path,
    monkeypatch,
):
    opened = admission_incidents.observe(
        admission_incidents.Faulted("invalid_json", "enforce", 7),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission_incidents, "secure_write_root", fail_write)
    admission_incidents.observe(
        admission_incidents.IntakeRestored("enforce", 7),
        notify=lambda _event: None,
        state_dir=tmp_path,
    )
    heartbeat_incident = admission_incidents.snapshot(state_dir=tmp_path)
    assert heartbeat_incident["active"] is False
    assert heartbeat_incident["incident_id"] == opened.incident_id
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "state": "idle",
                "updated_at": heartbeat_incident["recovered_at"],
                "admission_incident": heartbeat_incident,
            }
        ),
        encoding="utf-8",
    )
    importlib.reload(admission_incidents)

    health = client.get("/api/health").json()
    operator = client.get("/api/autopilot").json()["task_admission_incident"]

    assert health["status"] == "ok"
    assert health["intake_available"] is True
    assert health["error_code"] == ""
    assert operator["active"] is False
    assert operator["sequence"] == heartbeat_incident["sequence"]
