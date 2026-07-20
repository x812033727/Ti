"""觀察窗 preflight：來源、政策、演練、quiescence 與 hash 證據。"""

from __future__ import annotations

import json
import subprocess

import pytest

from studio import autonomy, autonomy_preflight, backlog, config, notify, projects


def _verified_drill_payload():
    return {
        "drill": True,
        "drill_verified": True,
        "dry_run": True,
        "backup_sha": "a" * 40,
        "scope_limit": "single_head_commit_exact_previous_tree",
        "mechanism": "isolated_git_revert_exact_previous_tree",
    }


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def state(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "baseline")
    _git(source, "remote", "add", "origin", "https://github.com/example/ti.git")

    monkeypatch.setattr(config, "PROJECT_ROOT", source)
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_DIR", source)
    monkeypatch.setattr(config, "AUTOPILOT_WORK_DIR", tmp_path / "worker")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "example/ti")
    monkeypatch.setattr(config, "PUBLISH_REPO", "example/product")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path, source


def _prepare_green_stage3(state, monkeypatch):
    tmp_path, source = state
    project = projects.create("product")
    autonomy.ensure_policy(
        autonomy.CORE_PROJECT_ID,
        source={
            "repo": "example/ti",
            "workspace": str(config.AUTOPILOT_WORK_DIR),
            "publish_repo": "example/ti",
            "lane": "main",
        },
    )
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"limits": {"daily_pr": 20}})
    autonomy.set_platform_mode([autonomy.CORE_PROJECT_ID, project["id"]], "shadow")
    autonomy.save_policy(
        project["id"],
        {
            "limits": {"daily_pr": 10},
            "deployment": {
                "health_url": "https://product.example/healthz",
                "healthy_field": "ok",
                "revision_field": "build.git_sha",
            },
        },
    )
    monkeypatch.setattr(notify, "_post_webhook", lambda *args, **kwargs: True)
    assert notify.send_red_drills()["ok"] is True
    autonomy.emit_event(
        "rollback_result",
        project_id=autonomy.CORE_PROJECT_ID,
        outcome="success",
        payload=_verified_drill_payload(),
    )
    autonomy.emit_event(
        "rollback_result",
        project_id=project["id"],
        outcome="success",
        payload=_verified_drill_payload(),
    )
    return project, source


def test_green_stage3_preflight_is_hashed_and_contains_no_workspace_path(state, monkeypatch):
    project, source = _prepare_green_stage3(state, monkeypatch)
    report = autonomy_preflight.collect(
        project_rows=[project], deployed_dir=source, source_dir=source, now=1000
    )
    assert report["stage3_observation"]["ready"] is True
    assert report["stage3_observation"]["blocking_reasons"] == []
    assert autonomy_preflight.verify_report(report)
    assert str(config.AUTOPILOT_WORK_DIR) not in json.dumps(report)


def test_preflight_fails_closed_on_drift_active_task_and_missing_controls(state):
    tmp_path, source = state
    backlog.add("running", state_dir=config.AUTOPILOT_STATE_DIR)
    task = backlog.next_pending(state_dir=config.AUTOPILOT_STATE_DIR)
    backlog.set_status(task["id"], "in_progress", state_dir=config.AUTOPILOT_STATE_DIR)
    other = tmp_path / "other"
    other.mkdir()
    report = autonomy_preflight.collect(
        project_rows=[], deployed_dir=source, source_dir=other, now=1000
    )
    blockers = set(report["stage3_observation"]["blocking_reasons"])
    assert {
        "source_baseline_aligned",
        "all_tasks_quiescent",
        "all_policies_and_sources_complete",
        "platform_shadow_rollout_aligned",
        "all_red_drills_within_5m",
        "rollback_drill_succeeded",
    } <= blockers
    assert report["stage3_observation"]["ready"] is False


def test_preflight_rejects_dirty_source_even_when_head_sha_matches(state, monkeypatch):
    project, source = _prepare_green_stage3(state, monkeypatch)
    (source / "README.md").write_text("uncommitted change\n", encoding="utf-8")
    report = autonomy_preflight.collect(
        project_rows=[project], deployed_dir=source, source_dir=source, now=1000
    )
    check = report["stage3_observation"]["checks"]["source_baseline_aligned"]
    assert check["ok"] is False
    assert check["evidence"]["source_worktree_clean"] is False
    assert "source_baseline_aligned" in report["stage3_observation"]["blocking_reasons"]


def test_preflight_defaults_to_actual_autopilot_source_worktree(state, monkeypatch):
    project, deployed = _prepare_green_stage3(state, monkeypatch)
    worker = config.AUTOPILOT_WORK_DIR
    _git(state[0], "clone", str(deployed), str(worker))
    _git(worker, "remote", "set-url", "origin", "https://github.com/example/ti.git")

    report = autonomy_preflight.collect(project_rows=[project], deployed_dir=deployed, now=1000)
    assert report["stage3_observation"]["ready"] is True

    (worker / "IN_FLIGHT").write_text("dirty\n", encoding="utf-8")
    report = autonomy_preflight.collect(project_rows=[project], deployed_dir=deployed, now=1000)
    check = report["stage3_observation"]["checks"]["source_baseline_aligned"]
    assert check["ok"] is False
    assert check["evidence"]["deployed_worktree_clean"] is True
    assert check["evidence"]["source_worktree_clean"] is False


def test_write_report_persists_verifiable_red_or_green_snapshot(state, monkeypatch):
    project, source = _prepare_green_stage3(state, monkeypatch)
    report = autonomy_preflight.write_report(
        project_rows=[project], deployed_dir=source, source_dir=source, now=1000
    )
    files = list((config.AUTOPILOT_STATE_DIR / "autonomy" / "preflight-reports").glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored == report and autonomy_preflight.verify_report(stored)
    stored["stage3_observation"]["ready"] = not stored["stage3_observation"]["ready"]
    assert autonomy_preflight.verify_report(stored) is False
    assert any(
        event.get("outcome") == "preflight_snapshot_created" for event in autonomy.read_events(1)
    )


def test_stage4_preflight_requires_external_project_health_revision_contract(state, monkeypatch):
    project, source = _prepare_green_stage3(state, monkeypatch)
    autonomy.save_policy(project["id"], {"deployment": {"health_url": ""}})
    report = autonomy_preflight.collect(
        project_rows=[project], deployed_dir=source, source_dir=source, now=1000
    )
    check = report["stage4_observation"]["checks"]["all_deployment_health_contracts_ready"]
    assert check["ok"] is False
    assert check["evidence"]["ready_projects"] == [autonomy.CORE_PROJECT_ID]

    autonomy.save_policy(
        project["id"],
        {
            "deployment": {
                "health_url": "https://product.example/healthz",
                "healthy_field": "ok",
                "revision_field": "build.git_sha",
            }
        },
    )
    report = autonomy_preflight.collect(
        project_rows=[project], deployed_dir=source, source_dir=source, now=1000
    )
    assert (
        report["stage4_observation"]["checks"]["all_deployment_health_contracts_ready"]["ok"]
        is True
    )
