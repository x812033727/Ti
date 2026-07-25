"""管理端 admission override API：scope CAS、理由與安全阻擋。"""

from __future__ import annotations

import json

import pytest

from studio import backlog, config, routes, task_admission


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state_dir)
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_DIR", repo)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    task = task_admission.enqueue_task(
        "模糊人工任務",
        source="manual",
        risk="low",
        mode="enforce",
        repo_context={"root": repo, "repo_sha": "a" * 40},
    )
    return {"repo": repo, "task": task}


@pytest.mark.asyncio
async def test_override_api_success_and_human_audit(state):
    task = state["task"]
    response = await routes.autopilot_admission_override(
        task["id"],
        routes.AdmissionOverrideBody(
            scope_hash=task["admission"]["scope_hash"],
            reason="接受低風險預設並繼續",
        ),
    )

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["task"]["status"] == "pending"
    assert body["task"]["admission"]["outcome"] == "ready"
    assert body["task"].get("human_approved") is not True
    audit = [
        json.loads(line)
        for line in (config.AUTOPILOT_STATE_DIR / "admission_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert audit[-1]["human_review"] is True
    assert audit[-1]["rule_ids"] == ["admin_quality_override"]
    interventions = [
        json.loads(line)
        for line in (config.AUTOPILOT_STATE_DIR / "interventions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert "接受低風險預設並繼續" not in json.dumps(
        interventions,
        ensure_ascii=False,
    )
    assert interventions[-1]["detail"].startswith("reason_sha256:")


@pytest.mark.asyncio
async def test_override_api_validates_reason_scope_and_conflicts(state):
    task = state["task"]
    empty = await routes.autopilot_admission_override(
        task["id"],
        routes.AdmissionOverrideBody(
            scope_hash=task["admission"]["scope_hash"],
            reason=" ",
        ),
    )
    malformed = await routes.autopilot_admission_override(
        task["id"],
        routes.AdmissionOverrideBody(scope_hash="bad", reason="有理由"),
    )
    stale = await routes.autopilot_admission_override(
        task["id"],
        routes.AdmissionOverrideBody(scope_hash="0" * 64, reason="有理由"),
    )
    missing = await routes.autopilot_admission_override(
        999,
        routes.AdmissionOverrideBody(scope_hash="0" * 64, reason="有理由"),
    )

    assert empty.status_code == 400
    assert malformed.status_code == 400
    assert stale.status_code == 409
    assert missing.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "unpark"])
async def test_generic_task_action_cannot_bypass_enforce_admission_hold(state, action):
    task = state["task"]

    response = await routes.autopilot_task_action(
        task["id"],
        routes.TaskActionBody(action=action),
    )

    assert response.status_code == 409
    assert backlog.get(task["id"])["status"] == "parked"


@pytest.mark.asyncio
async def test_override_redacts_secret_like_reason_before_persisting(state):
    task = state["task"]
    response = await routes.autopilot_admission_override(
        task["id"],
        routes.AdmissionOverrideBody(
            scope_hash=task["admission"]["scope_hash"],
            reason="採用預設 token=super-secret-value",
        ),
    )

    encoded = response.body.decode()
    assert response.status_code == 200
    assert "super-secret-value" not in encoded
    assert "[REDACTED]" in encoded


@pytest.mark.asyncio
async def test_admission_audit_api_returns_sanitized_records_metrics_and_circuit(state):
    response = await routes.autopilot_admission_audit(limit=50)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["records"]
    assert body["metrics"]["total"] >= 1
    assert body["metrics"]["no_llm_rate"] == 1.0
    assert body["circuit"]["paused"] is False
    encoded = json.dumps(body, ensure_ascii=False)
    assert "current_contract" not in encoded
    assert "detail" not in body["records"][0]
