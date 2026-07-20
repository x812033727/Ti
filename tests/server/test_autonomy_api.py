"""自治狀態/政策 API：向後相容 additive 指標與 admin 寫入門禁。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import autonomy, config, notify, projects


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def test_status_and_policy_roundtrip(client):
    project = projects.create("Line bot")
    status = client.get("/api/autonomy/status")
    assert status.status_code == 200
    data = status.json()
    assert data["schema_version"] == 1
    row = next(p for p in data["projects"] if p["project_id"] == project["id"])
    assert row["mode"] == "shadow" and row["blocking_reasons"] == []
    assert row["stage"] == 2 and row["target_stage"] == 2
    assert data["platform"]["daily_cost_hard_limit_usd"] == 100.0
    assert "webhook_configured" in data["platform"]["notification"]

    updated = client.put(
        f"/api/autonomy/policies/{project['id']}",
        json={"mode": "canary", "stage": 3},
    )
    assert updated.status_code == 200
    assert updated.json()["policy"]["mode"] == "canary"
    assert client.get(f"/api/autonomy/policies/{project['id']}").json()["policy"]["revision"] == 1


def test_preflight_read_and_admin_snapshot_contract(client):
    preview = client.get("/api/autonomy/preflight")
    assert preview.status_code == 200
    assert preview.json()["calculation_version"] == "autonomy-preflight-v1"
    assert "blocking_reasons" in preview.json()["stage3_observation"]

    saved = client.post("/api/autonomy/preflight/snapshot")
    assert saved.status_code == 200 and saved.json()["ok"] is True
    assert saved.json()["report"]["report_hash"]


def test_admin_rollback_drill_endpoint_is_local_and_structured(client, monkeypatch):
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    called = []

    def fake_drill(project_id, workspace):
        called.append((project_id, workspace))
        return {
            "project_id": project_id,
            "ok": True,
            "reason": "verified",
            "source_sha": "a" * 40,
            "backup_sha": "b" * 40,
            "event_id": "event-1",
        }

    monkeypatch.setattr(autonomy, "run_rollback_drill", fake_drill)
    response = client.post("/api/autonomy/rollback-drills")
    assert response.status_code == 200 and response.json()["ok"] is True
    assert [row[0] for row in called] == [autonomy.CORE_PROJECT_ID]


def test_platform_mode_updates_core_and_all_projects_together(client):
    first = projects.create("first")
    second = projects.create("second")
    response = client.put("/api/autonomy/platform-mode", json={"mode": "shadow"})
    assert response.status_code == 200
    rollout = response.json()["rollout"]
    assert rollout["aligned"] is True
    assert set(rollout["project_ids"]) == {
        autonomy.CORE_PROJECT_ID,
        first["id"],
        second["id"],
    }
    assert all(autonomy.load_policy(pid)["mode"] == "shadow" for pid in rollout["project_ids"])


def test_canary_rollout_api_requires_green_preflight(client):
    project = projects.create("not-ready-canary")
    autonomy.save_policy(project["id"], {"stage": 3})
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"stage": 3})
    response = client.put("/api/autonomy/platform-mode", json={"mode": "canary"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Stage 3 preflight 尚未全綠"
    assert response.json()["blocking_reasons"]


def test_formal_promotion_endpoint_refuses_red_maturity(client):
    project = projects.create("not-ready")
    autonomy.save_policy(project["id"], {"stage": 3})
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"stage": 3})
    response = client.post("/api/autonomy/promote", json={"stage": 3})
    assert response.status_code == 409
    assert "成熟度尚未全綠" in response.json()["detail"]
    assert autonomy.official_stage(project["id"]) == 2


def test_invalid_policy_and_unknown_project(client):
    project = projects.create("x")
    bad = client.put(f"/api/autonomy/policies/{project['id']}", json={"mode": "unsafe"})
    assert bad.status_code == 422
    assert client.get("/api/autonomy/policies/nope").status_code == 404


def test_metrics_and_audit_trend_expose_additive_autonomy_contract(client):
    metrics = client.get("/api/metrics").json()
    assert metrics["autonomy"]["calculation_version"] == autonomy.CALCULATION_VERSION
    trend = client.get("/api/autopilot/audit-trend?days=28").json()
    assert trend["autonomy"]["eligible"] == 0
    assert "promotion" in trend["autonomy"]


def test_policy_write_requires_admin_for_public_peer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    from studio.server import app

    project = projects.create("x")
    public = TestClient(app, client=("203.0.113.5", 40000))
    response = public.put(f"/api/autonomy/policies/{project['id']}", json={"mode": "canary"})
    assert response.status_code == 403


def test_events_and_brake_clear_contract(client):
    project = projects.create("x")
    autonomy.trip_brake("project", "test_violation", project_id=project["id"])
    response = client.post("/api/autonomy/brakes/project/clear", json={"project_id": project["id"]})
    assert response.status_code == 200 and response.json() == {"ok": True, "changed": True}
    assert autonomy.admission_decision(project["id"])["allowed"] is True
    events = client.get("/api/autonomy/events?include_legacy=false").json()
    assert events["schema_version"] == 1
    assert {row["outcome"] for row in events["events"]} >= {
        "brake_tripped",
        "brake_cleared",
    }


def test_red_drills_endpoint_is_admin_only_and_covers_all_kinds(client, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    monkeypatch.setattr(notify, "_post_webhook", lambda *args, **kwargs: True)
    response = client.post("/api/notify/red-drills")
    assert response.status_code == 200 and response.json()["ok"] is True
    assert set(response.json()["results"]) == set(notify.RED_DRILL_KINDS)
