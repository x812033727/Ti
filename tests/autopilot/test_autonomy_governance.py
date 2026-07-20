"""自治 v1 契約：政策、基線、風險、eligible 分母、煞車與不可竄改報告。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time

import pytest

from studio import autonomy, backlog, config, deploy, interventions, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def _baseline(**overrides):
    row = {
        "deployed_sha": "a" * 40,
        "source_sha": "a" * 40,
        "source_repo": "owner/repo",
        "workspace": "/work/project",
        "base_branch": "main",
        "lane": "main",
        "publish_repo": "owner/repo",
        "source_worktree_clean": True,
        "deployed_identity_verified": True,
        "eligible": True,
        "exclusion_reason": "",
        "risk": "medium",
    }
    row.update(overrides)
    return row


def _observed_baseline(**overrides):
    row = {
        "deployed_sha": "a" * 40,
        "source_sha": "a" * 40,
        "source_repo": "owner/repo",
        "workspace": "/work/project",
        "base_branch": "main",
        "lane": "main",
        "publish_repo": "owner/repo",
        "deployed_identity_verified": True,
        "deployed_worktree_clean": True,
    }
    row.update(overrides)
    return row


def _verified_drill_payload():
    return {
        "drill": True,
        "drill_verified": True,
        "dry_run": True,
        "backup_sha": "a" * 40,
        "scope_limit": "single_head_commit_exact_previous_tree",
        "mechanism": "isolated_git_revert_exact_previous_tree",
    }


def test_policy_defaults_shadow_and_updates_are_versioned():
    policy = autonomy.ensure_policy("p1")
    assert policy["mode"] == "shadow" and policy["schema_version"] == 1
    updated = autonomy.save_policy(
        "p1",
        {
            "mode": "canary",
            "intent": {
                "north_star": "讓部署健康率達 99.9%",
                "success_metrics": ["deploy_slo>=0.999"],
                "forbidden_actions": ["delete production data"],
            },
        },
    )
    assert updated["revision"] == 1 and updated["intent"]["version"] == 2
    assert autonomy.load_policy("p1")["intent"]["north_star"].startswith("讓部署")
    with pytest.raises(autonomy.PolicyError):
        autonomy.save_policy("p1", {"mode": "god-mode"})
    with pytest.raises(autonomy.PolicyError):
        autonomy.save_policy("p1", {"webhook_token": "secret"})
    with pytest.raises(autonomy.PolicyError):
        autonomy.save_policy("p1", {"source": {"token": "secret"}})


def test_stage4_policy_requires_complete_versioned_intent():
    autonomy.ensure_policy("p1")
    with pytest.raises(autonomy.PolicyError, match="完整 intent"):
        autonomy.save_policy("p1", {"stage": 4})
    policy = autonomy.save_policy(
        "p1",
        {
            "stage": 4,
            "intent": {
                "north_star": "健康部署",
                "success_metrics": ["deploy_health=1"],
                "forbidden_actions": ["不可刪除正式資料"],
            },
        },
    )
    assert policy["stage"] == 4
    assert autonomy.stage4_planner_status("p1")["ready"] is True


def test_deploy_phase_risk_only_escalates_and_never_downgrades():
    assert autonomy.phase_risk("medium", "deploy", 3) == "high-reversible"
    assert autonomy.phase_risk("high-reversible", "deploy", 3) == "high-reversible"
    assert autonomy.phase_risk("irreversible", "deploy", 3) == "irreversible"
    assert autonomy.phase_risk("unknown", "deploy", 4) == "irreversible"


def test_event_schema_and_legacy_unknowns_are_explicit(tmp_path):
    event = autonomy.emit_event("autonomy_decision", project_id="p1", outcome="run_started")
    required = {
        "schema_version",
        "event_id",
        "event_type",
        "ts",
        "run_id",
        "project_id",
        "task_id",
        "source_sha",
        "risk",
        "eligible",
        "intervention_type",
        "approval_result",
        "cost_usd",
        "outcome",
    }
    assert required <= set(event)
    assert event["eligible"] == "unknown" and event["cost_usd"] == "unknown"

    state = tmp_path / "ap"
    state.mkdir()
    (state / "audit.jsonl").write_text(
        json.dumps({"ts": time.time(), "task_id": 3, "outcome": "merged"}) + "\n",
        encoding="utf-8",
    )
    legacy = autonomy.legacy_events(7)
    assert legacy[0]["eligible"] == "unknown"
    assert legacy[0]["cost_usd"] == "unknown"
    assert legacy[0]["source_sha"] == "unknown"


def test_baseline_sha_or_repo_drift_fails_closed_and_trips_global_brake():
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"mode": "canary"})
    result = autonomy.begin_run(
        autonomy.CORE_PROJECT_ID,
        7,
        _baseline(source_sha="b" * 40, publish_repo="other/repo"),
        run_id="r-drift",
    )
    assert result["allowed"] is False
    assert {"source_sha_drift", "source_publish_repo_mismatch"} <= set(result["reasons"])
    admission = autonomy.admission_decision(autonomy.CORE_PROJECT_ID)
    assert admission["allowed"] is False
    assert admission["reasons"][0].startswith("global_brake:")


def test_dirty_or_unverified_baseline_fails_closed():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    result = autonomy.begin_run(
        "p1",
        8,
        _baseline(source_worktree_clean=False, deployed_identity_verified=False),
        run_id="dirty-baseline",
    )
    assert result["allowed"] is False
    assert {"source_worktree_dirty", "deployed_identity_unverified"} <= set(result["reasons"])


def test_expected_workspace_lane_and_repo_are_part_of_baseline():
    autonomy.ensure_policy("p1")
    autonomy.save_policy(
        "p1",
        {
            "mode": "canary",
            "source": {
                "repo": "owner/repo",
                "publish_repo": "owner/repo",
                "workspace": "/expected/workspace",
                "lane": "main",
            },
        },
    )
    result = autonomy.begin_run(
        "p1",
        9,
        _baseline(workspace="/wrong/workspace", lane="side"),
        run_id="wrong-workspace",
    )
    assert result["allowed"] is False
    assert {"workspace_mismatch", "lane_mismatch"} <= set(result["reasons"])


def test_excluded_task_requires_reason_but_old_unknown_is_not_faked_green():
    autonomy.ensure_policy("p1")
    blocked = autonomy.begin_run(
        "p1", 1, _baseline(eligible=False, exclusion_reason=""), run_id="bad-exclusion"
    )
    assert blocked["allowed"] is True, "shadow 只記 warning，不外寫也不觸發煞車"
    warnings = autonomy.read_events(1)[-1]["payload"]["shadow_warnings"]
    assert "exclusion_reason_required" in warnings

    autonomy.emit_event(
        "autonomy_decision",
        run_id="legacy-like",
        project_id="p1",
        outcome="run_started",
        eligible="unknown",
    )
    metrics = autonomy.maturity_metrics(28, project_ids=["p1"])
    assert metrics["eligible"] == 0
    assert metrics["unknown_eligibility"] == 1
    assert metrics["zero_touch_rate"] is None


def test_canary_rejects_task_when_eligibility_was_not_decided_before_start():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    blocked = autonomy.begin_run(
        "p1",
        2,
        _baseline(eligible="unknown"),
        run_id="unknown-eligibility",
    )
    assert blocked["allowed"] is False
    assert "eligibility_undecided" in blocked["reasons"]


def test_external_write_revalidation_allows_only_the_pinned_live_baseline():
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.save_policy(
        autonomy.CORE_PROJECT_ID,
        {
            "mode": "canary",
            "source": {
                "repo": "owner/repo",
                "workspace": "/work/project",
                "publish_repo": "owner/repo",
                "lane": "main",
            },
        },
    )
    result = autonomy.revalidate_run_baseline(
        autonomy.CORE_PROJECT_ID,
        _baseline(),
        _observed_baseline(),
        phase="pre_merge",
        run_id="live-exact",
        task_id=7,
    )
    assert result == {"allowed": True, "reasons": [], "shadow_warnings": []}
    event = autonomy.read_events(1)[-1]
    assert event["outcome"] == "baseline_revalidated"
    assert event["payload"]["observed_source_sha"] == "a" * 40
    assert autonomy.brake_status()["global"] is None


def test_external_write_source_drift_fails_closed_and_trips_core_global_brake():
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.save_policy(
        autonomy.CORE_PROJECT_ID,
        {
            "mode": "canary",
            "source": {
                "repo": "owner/repo",
                "workspace": "/work/project",
                "publish_repo": "owner/repo",
                "lane": "main",
            },
        },
    )
    result = autonomy.revalidate_run_baseline(
        autonomy.CORE_PROJECT_ID,
        _baseline(),
        _observed_baseline(source_sha="b" * 40),
        phase="pre_merge",
        run_id="live-drift",
        task_id=8,
    )
    assert result["allowed"] is False
    assert {"source_sha_drift", "source_deployed_sha_mismatch"} <= set(result["reasons"])
    assert autonomy.brake_status()["global"]["active"] is True
    events = autonomy.read_events(1)
    blocked = next(e for e in events if e.get("outcome") == "baseline_revalidation_blocked")
    assert blocked["payload"]["pinned_source_sha"] == "a" * 40
    assert blocked["payload"]["observed_source_sha"] == "b" * 40


def test_external_project_drift_only_trips_that_project_brake():
    autonomy.ensure_policy("p1")
    autonomy.save_policy(
        "p1",
        {
            "mode": "canary",
            "source": {
                "repo": "owner/repo",
                "workspace": "/work/project",
                "publish_repo": "owner/repo",
                "lane": "main",
            },
        },
    )
    result = autonomy.revalidate_run_baseline(
        "p1",
        _baseline(),
        _observed_baseline(deployed_sha=""),
        phase="pre_deploy",
        run_id="project-drift",
        task_id=9,
    )
    assert result["allowed"] is False
    brakes = autonomy.brake_status()
    assert brakes["global"] is None
    assert brakes["projects"]["p1"]["active"] is True


def test_ai_batch_task_cannot_self_sign_irreversible_approval():
    assert (
        backlog.add_items(
            [
                {
                    "title": "rotate root credentials",
                    "risk": "irreversible",
                    "human_approved": True,
                }
            ],
            source="discovered",
        )
        == 1
    )
    task = backlog.list_tasks()[0]
    assert task["risk"] == "irreversible"
    assert task.get("human_approved") is not True


def _approval(provider, *, diff="d1", evidence="e1", verdict="approve"):
    return {
        "provider": provider,
        "diff_sha": diff,
        "evidence_sha": evidence,
        "verdict": verdict,
        "rationale": "reviewed the same diff and test evidence",
    }


def _high_operation(**overrides):
    row = {
        "risk": "high-reversible",
        "diff_sha": "d1",
        "evidence_sha": "e1",
        "rollback": {
            "dry_run": True,
            "backup": True,
            "verified": True,
            "scope_limit": "one canary instance",
        },
    }
    row.update(overrides)
    return row


def test_high_reversible_requires_two_distinct_providers_same_diff_and_evidence():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    good = autonomy.evaluate_operation(
        "p1",
        "deploy",
        _high_operation(),
        approvals=[_approval("openai"), _approval("anthropic")],
        run_id="r1",
    )
    assert good["external_write_allowed"] is True and good["dual_approval_ok"] is True

    same_provider = autonomy.evaluate_operation(
        "p1",
        "deploy",
        _high_operation(),
        approvals=[_approval("openai"), _approval("openai")],
        run_id="r2",
    )
    assert same_provider["allowed"] is False
    assert "providers_must_be_distinct" in same_provider["reasons"]

    missing_rollback = autonomy.evaluate_operation(
        "p1",
        "deploy",
        _high_operation(rollback={}),
        approvals=[_approval("openai"), _approval("anthropic")],
        run_id="r3",
    )
    assert missing_rollback["allowed"] is False
    assert "verified_rollback_required" in missing_rollback["reasons"]

    mismatched = autonomy.evaluate_operation(
        "p1",
        "deploy",
        _high_operation(),
        approvals=[_approval("openai"), _approval("anthropic", diff="other")],
        run_id="r4",
    )
    assert mismatched["allowed"] is False and "diff_mismatch" in mismatched["reasons"]

    planning = autonomy.evaluate_operation("p1", "planning", _high_operation(), run_id="r5")
    assert planning["allowed"] is True, "最終 diff 尚未產生前先驗 rollback，雙核可留給 merge/deploy"


def test_irreversible_and_unknown_require_human_approval():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "full"})
    no = autonomy.evaluate_operation("p1", "change", {"risk": "irreversible"})
    assert no["allowed"] is False and no["human_approval_required"] is True
    yes = autonomy.evaluate_operation("p1", "change", {"risk": "irreversible"}, human_approved=True)
    assert yes["allowed"] is True
    unknown = autonomy.evaluate_operation("p1", "planning", {"risk": "unknown"})
    assert unknown["allowed"] is False
    assert unknown["risk_reason"] == "unknown_risk_escalated"


def test_metrics_use_eligible_denominator_intervention_types_and_cost():
    for rid, outcome, cost in (("r1", "done", 3.0), ("r2", "failed", 2.0)):
        autonomy.emit_event(
            "autonomy_decision",
            run_id=rid,
            project_id="p1",
            eligible=True,
            outcome="run_started",
        )
        autonomy.record_run_outcome(
            rid, "p1", rid, outcome, eligible=True, cost_usd=cost, risk="medium"
        )
    interventions.record(
        "fix",
        "bug_design_fix",
        run_id="r1",
        project_id="p1",
        intervention_type="bug_design_fix",
    )
    metrics = autonomy.maturity_metrics(28, project_ids=["p1"])
    assert metrics["eligible"] == 2 and metrics["completed"] == 1
    assert metrics["completion_rate"] == 0.5 and metrics["zero_touch"] == 0
    assert metrics["interventions"]["by_type"]["bug_design_fix"] == 1
    assert metrics["interventions"]["by_path"] == {"bug_design_fix:fix": 1}
    assert metrics["cost"]["known_usd"] == 5.0
    assert metrics["cost"]["unknown_runs"] == 0
    assert metrics["cost"]["max_daily_usd"] == 5.0
    assert metrics["cost"]["daily_hard_limit_usd"] == 100.0


def test_negative_or_nonfinite_cost_is_unknown_and_cannot_offset_budget():
    for rid, cost in (("negative", -10.0), ("infinite", float("inf"))):
        autonomy.emit_event(
            "autonomy_decision",
            run_id=rid,
            project_id="p1",
            eligible=True,
            outcome="run_started",
        )
        event = autonomy.record_run_outcome(rid, "p1", rid, "done", eligible=True, cost_usd=cost)
        assert event["cost_usd"] == "unknown"
    metrics = autonomy.maturity_metrics(28, project_ids=["p1"])
    assert metrics["cost"]["known_usd"] == 0.0
    assert metrics["cost"]["unknown_runs"] == 2


def test_project_daily_pr_budget_counts_v1_terminal_and_deduplicates_legacy():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary", "limits": {"daily_pr": 1}})
    autonomy.emit_event(
        "autonomy_decision",
        run_id="pr-run",
        project_id="p1",
        task_id=3,
        eligible=True,
        outcome="run_started",
    )
    autonomy.record_run_outcome(
        "pr-run", "p1", 3, "merged", eligible=True, cost_usd=1.0, payload={"pr": 42}
    )
    state = config.AUTOPILOT_STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    (state / "audit.jsonl").write_text(
        json.dumps(
            {
                "ts": time.time(),
                "run_id": "pr-run",
                "project_id": "p1",
                "task_id": 3,
                "outcome": "merged",
                "pr": 42,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    admission = autonomy.admission_decision("p1")
    assert admission["allowed"] is False
    assert autonomy.brake_status()["projects"]["p1"]["reason"] == "daily_pr_budget_exceeded:1"


def test_platform_daily_pr_budget_counts_prs_across_projects():
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"limits": {"daily_pr": 2}})
    for pid, pr in (("p1", 10), ("p2", 11)):
        autonomy.record_run_outcome(
            f"run-{pid}",
            pid,
            1,
            "merged",
            eligible=True,
            cost_usd=1.0,
            payload={"pr": pr},
        )
    admission = autonomy.admission_decision("p3")
    assert admission["allowed"] is False
    assert autonomy.brake_status()["global"]["reason"] == "platform_daily_pr_budget_exceeded:2"


def test_slo_violation_is_paired_with_degrade_within_five_minutes():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "full"})
    result = autonomy.enforce_slo_violation(
        "p1", metric="availability", observed=0.95, threshold=0.99
    )
    assert result["controlled"] is True and result["action"] == "degraded"
    assert autonomy.load_policy("p1")["mode"] == "degraded"
    control = autonomy.maturity_metrics(28, project_ids=["p1"])["control_actions"]
    assert control["slo_violations"] == 1
    assert control["controlled_within_5m"] == 1
    assert control["coverage_rate"] == 1.0


def test_unmanaged_slo_violation_cannot_be_counted_as_controlled():
    result = autonomy.enforce_slo_violation(
        "p1", metric="availability", observed=0.95, threshold=0.99
    )
    assert result["controlled"] is False
    control = autonomy.maturity_metrics(28, project_ids=["p1"])["control_actions"]
    assert control["violations"] == 1 and control["auto_braked"] == 0
    assert control["coverage_rate"] == 0.0


def test_stage4_project_slo_violation_auto_degrades_once_per_day(monkeypatch):
    autonomy.ensure_policy("p1")
    autonomy.save_policy(
        "p1",
        {
            "mode": "full",
            "stage": 4,
            "intent": {
                "north_star": "healthy deploy",
                "success_metrics": ["closed_loop>=0.85"],
                "forbidden_actions": ["no destructive migration"],
            },
            "limits": {"closed_loop_slo_min": 0.85, "slo_min_eligible": 10},
        },
    )
    sent = []
    monkeypatch.setattr(notify, "send_bg", lambda kind, title, **kw: sent.append(kind))
    for index in range(10):
        rid = f"slo-run-{index}"
        autonomy.emit_event(
            "autonomy_decision",
            run_id=rid,
            project_id="p1",
            eligible=True,
            outcome="run_started",
        )
        autonomy.record_run_outcome(
            rid,
            "p1",
            index,
            "healthy_deployed" if index < 8 else "done",
            eligible=True,
            cost_usd=1.0,
        )
    first = autonomy.evaluate_slo_controls(["p1"])
    second = autonomy.evaluate_slo_controls(["p1"])
    assert first["projects"]["p1"]["controlled"] is True
    assert second["projects"]["p1"]["deduplicated"] is True
    assert autonomy.load_policy("p1")["mode"] == "degraded"
    assert sent.count("slo_brake") == 1


def test_legacy_intervention_fallback_prevents_false_zero_touch(monkeypatch):
    autonomy.emit_event(
        "autonomy_decision",
        run_id="r-fallback",
        project_id="p1",
        task_id=7,
        eligible=True,
        outcome="run_started",
    )
    autonomy.record_run_outcome(
        "r-fallback", "p1", 7, "done", eligible=True, cost_usd=1.0, risk="medium"
    )

    def audit_down(*args, **kwargs):
        raise autonomy.AuditWriteError("audit unavailable")

    monkeypatch.setattr(autonomy, "emit_event", audit_down)
    interventions.record(
        "manual_fix",
        "bug_design_fix",
        run_id="r-fallback",
        project_id="p1",
        task_id=7,
        intervention_type="bug_design_fix",
    )
    metrics = autonomy.maturity_metrics(28, project_ids=["p1"])
    assert metrics["interventions"]["by_type"]["bug_design_fix"] == 1
    assert metrics["zero_touch"] == 0


def test_notification_failure_is_observable_without_secret(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://secret.example/hook/token-value")
    monkeypatch.setattr(notify, "_post_webhook", lambda *args, **kwargs: False)
    assert notify.send("task_failed", "x", drill=True) is False
    row = notify.read_deliveries(1)[0]
    assert row["ok"] is False and row["error"] == "delivery_failed"
    encoded = json.dumps(row)
    assert "secret.example" not in encoded and "token-value" not in encoded


def test_notification_latency_measures_from_alert_creation_not_http_duration(monkeypatch):
    alert = {
        "event_id": "delayed-alert",
        "kind": "task_failed",
        "ts": time.time() - 301,
    }
    notify._persist_delivery(alert, "webhook", True, 0.02)
    row = notify.read_deliveries(1)[0]
    assert row["latency_s"] >= 301
    assert row["delivery_duration_s"] == 0.02


def test_negative_notification_latency_cannot_pass_five_minute_gate(monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        notify,
        "read_deliveries",
        lambda *args, **kwargs: [{"ts": now, "ok": True, "latency_s": -1}],
    )
    monkeypatch.setattr(notify, "read_events", lambda *args, **kwargs: [])
    assert autonomy.maturity_metrics(1)["alerts"]["delivered_within_5m"] == 0


def test_notification_payload_redacts_secret_fields_and_sensitive_paths(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    captured = {}

    def capture(_url, _kind, title, extra):
        captured.update({"title": title, "extra": extra})
        return True

    monkeypatch.setattr(notify, "_post_webhook", capture)
    assert notify.send(
        "task_failed",
        "失敗於 /root/private/repo",
        api_token="top-secret",
        detail="log at /opt/ti/internal/file.py",
    )
    encoded = json.dumps(captured, ensure_ascii=False)
    assert "top-secret" not in encoded
    assert "/root/private" not in encoded and "/opt/ti" not in encoded


def test_all_required_red_drills_are_measured_by_kind(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    monkeypatch.setattr(notify, "_post_webhook", lambda *args, **kwargs: True)
    result = notify.send_red_drills()
    assert result["ok"] is True
    assert set(result["results"]) == set(notify.RED_DRILL_KINDS)
    metrics = autonomy.maturity_metrics(28)
    assert metrics["alerts"]["red_drills_complete"] is True
    assert metrics["alerts"]["red_drill_coverage_rate"] == 1.0


def test_rejected_dual_review_does_not_count_as_high_risk_coverage():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    autonomy.emit_event(
        "autonomy_decision",
        run_id="bad-review",
        project_id="p1",
        task_id=5,
        eligible=True,
        risk="high-reversible",
        outcome="run_started",
    )
    decision = autonomy.evaluate_operation(
        "p1",
        "deploy",
        _high_operation(),
        approvals=[_approval("openai"), _approval("anthropic", diff="wrong")],
        run_id="bad-review",
        task_id=5,
    )
    assert decision["allowed"] is False
    autonomy.record_run_outcome(
        "bad-review",
        "p1",
        5,
        "blocked",
        eligible=True,
        cost_usd=0.1,
        risk="high-reversible",
    )
    approval = autonomy.maturity_metrics(28, project_ids=["p1"])["high_risk_approval"]
    assert approval == {"operations": 1, "dual_provider_covered": 0, "coverage_rate": 0.0}


def test_merge_approval_does_not_cover_later_high_risk_deploy_gate():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    autonomy.emit_event(
        "autonomy_decision",
        run_id="two-gates",
        project_id="p1",
        task_id=6,
        eligible=True,
        risk="high-reversible",
        outcome="run_started",
    )
    assert autonomy.evaluate_operation(
        "p1",
        "merge",
        _high_operation(),
        approvals=[_approval("openai"), _approval("anthropic")],
        run_id="two-gates",
        task_id=6,
    )["allowed"]
    assert not autonomy.evaluate_operation(
        "p1",
        "deploy",
        _high_operation(),
        approvals=[_approval("openai"), _approval("anthropic", diff="wrong")],
        run_id="two-gates",
        task_id=6,
    )["allowed"]
    autonomy.record_run_outcome(
        "two-gates",
        "p1",
        6,
        "blocked",
        eligible=True,
        cost_usd=0.1,
        risk="high-reversible",
    )
    assert autonomy.maturity_metrics(28, project_ids=["p1"])["high_risk_approval"] == {
        "operations": 2,
        "dual_provider_covered": 1,
        "coverage_rate": 0.5,
    }


def test_cost_brake_and_report_hash_chain():
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary", "limits": {"daily_cost_usd": 5}})
    autonomy.emit_event(
        "autonomy_decision",
        run_id="r1",
        project_id="p1",
        eligible=True,
        cost_usd=6.0,
        outcome="failed",
    )
    assert autonomy.admission_decision("p1")["allowed"] is False
    report = autonomy.write_maturity_report(project_ids=["p1"])
    assert autonomy.verify_report(report) is True
    tampered = json.loads(json.dumps(report))
    tampered["metrics"]["eligible"] = 999
    assert autonomy.verify_report(tampered) is False
    report_path = (
        config.AUTOPILOT_STATE_DIR / "autonomy" / "maturity-reports" / f"{report['day']}.json"
    )
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(autonomy.AuditWriteError, match="完整性"):
        autonomy.write_maturity_report(project_ids=["p1"])


def test_project_brake_clear_starts_a_new_consecutive_failure_period():
    """根因修復後 clear 必須可恢復；但新期間再連敗三次仍要重新煞車。"""
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary", "limits": {"consecutive_failures": 3}})

    for idx in range(3):
        autonomy.record_run_outcome(
            f"old-failure-{idx}",
            "p1",
            idx,
            "failed",
            eligible=True,
            cost_usd=0.0,
        )

    first = autonomy.admission_decision("p1")
    assert first["allowed"] is False
    assert first["reasons"] == ["project_brake:consecutive_failures:3"]
    assert autonomy.clear_brake("project", project_id="p1", actor="root_cause_remediated")
    assert autonomy.admission_decision("p1")["allowed"] is True

    for idx in range(2):
        autonomy.record_run_outcome(
            f"new-failure-{idx}",
            "p1",
            idx + 10,
            "failed",
            eligible=True,
            cost_usd=0.0,
        )
        assert autonomy.admission_decision("p1")["allowed"] is True

    autonomy.record_run_outcome(
        "new-failure-2",
        "p1",
        12,
        "failed",
        eligible=True,
        cost_usd=0.0,
    )
    retripped = autonomy.admission_decision("p1")
    assert retripped["allowed"] is False
    assert retripped["reasons"] == ["project_brake:consecutive_failures:3"]


def test_maturity_report_seals_previous_complete_utc_day_only():
    now = time.time()
    current = time.gmtime(now)
    day_start = float(
        __import__("calendar").timegm((current.tm_year, current.tm_mon, current.tm_mday, 0, 0, 0))
    )
    for rid in ("yesterday", "today"):
        autonomy.emit_event(
            "autonomy_decision",
            run_id=rid,
            project_id="p1",
            eligible=True,
            outcome="run_started",
        )
        autonomy.record_run_outcome(
            rid, "p1", rid, "done", eligible=True, cost_usd=1.0, risk="medium"
        )
    path = config.PROJECTS_ROOT / "p1" / "autonomy-events.v1.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["ts"] = day_start - 60 if row["run_id"] == "yesterday" else day_start + 60
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    report = autonomy.write_maturity_report(now=day_start + 3600, project_ids=["p1"])
    assert report["day"] == time.strftime("%Y-%m-%d", time.gmtime(day_start - 1))
    assert report["period_end"] == day_start
    assert report["metrics"]["eligible"] == 1
    assert report["metrics"]["completed"] == 1


def test_rollback_failure_trips_only_project_brake():
    autonomy.ensure_policy("p1")
    autonomy.emit_event("rollback_result", project_id="p1", outcome="failed")
    assert autonomy.admission_decision("p1")["allowed"] is False
    assert autonomy.brake_status()["projects"]["p1"]["active"] is True
    assert autonomy.admission_decision("p2")["allowed"] is True


def test_rollback_readiness_requires_success_for_every_required_project():
    autonomy.emit_event(
        "rollback_result",
        project_id=autonomy.CORE_PROJECT_ID,
        outcome="success",
        payload=_verified_drill_payload(),
    )
    metrics = autonomy.maturity_metrics(28, project_ids=[autonomy.CORE_PROJECT_ID, "p1"])
    assert metrics["rollback"]["by_project"][autonomy.CORE_PROJECT_ID]["success"] == 1
    assert metrics["rollback"]["by_project"]["p1"]["success"] == 0
    assert metrics["promotion"]["stage3"]["checks"]["rollback_100pct_each_project"] is False
    autonomy.emit_event(
        "rollback_result", project_id="p1", outcome="success", payload=_verified_drill_payload()
    )
    metrics = autonomy.maturity_metrics(28, project_ids=[autonomy.CORE_PROJECT_ID, "p1"])
    assert metrics["promotion"]["stage3"]["checks"]["rollback_100pct_each_project"] is True


def test_plain_success_event_cannot_forge_verified_rollback_drill():
    autonomy.emit_event(
        "rollback_result",
        project_id="p1",
        outcome="success",
        payload={"drill": True},
    )
    metrics = autonomy.maturity_metrics(28, project_ids=["p1"])
    row = metrics["rollback"]["by_project"]["p1"]
    assert row["verified_drill_success"] == 0
    assert row["drill_failed"] == 1
    assert metrics["promotion"]["stage3"]["checks"]["rollback_100pct_each_project"] is False


def test_standard_deploy_uses_platform_verified_rollback_evidence():
    autonomy.emit_event(
        "rollback_result",
        project_id="p1",
        outcome="success",
        payload=_verified_drill_payload(),
    )
    evidence = autonomy.deployment_rollback_evidence("p1", "b" * 40)
    assert all(evidence[key] for key in ("dry_run", "backup", "verified", "scope_limit"))
    assert evidence["previous_healthy_revision"] == "b" * 40

    autonomy.emit_event(
        "rollback_result",
        project_id="p1",
        outcome="failed",
        payload={"drill": True, "drill_verified": False},
    )
    failed = autonomy.deployment_rollback_evidence("p1", "b" * 40)
    assert failed["verified"] is False


def test_local_rollback_drill_proves_exact_previous_tree(tmp_path):
    repo = tmp_path / "rollback-repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "value.txt").write_text("one\n", encoding="utf-8")
    git("add", "value.txt")
    git("commit", "-m", "one")
    (repo / "value.txt").write_text("two\n", encoding="utf-8")
    git("commit", "-am", "two")
    git("remote", "add", "origin", "https://github.com/example/product.git")
    autonomy.ensure_policy(
        "p1",
        source={
            "repo": "example/product",
            "workspace": str(repo),
            "publish_repo": "example/product",
            "lane": "main",
        },
    )

    result = autonomy.run_rollback_drill("p1", repo)
    assert result["ok"] is True and result["reason"] == "verified"
    assert git("status", "--porcelain") == ""
    event = autonomy.read_events(1)[-1]
    assert autonomy.verified_rollback_drill(event) is True


def test_platform_rollout_is_synchronized_but_project_downgrade_is_local():
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.ensure_policy("p1")
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"stage": 3})
    autonomy.save_policy("p1", {"stage": 3})
    rollout = autonomy.set_platform_mode([autonomy.CORE_PROJECT_ID, "p1"], "canary")
    assert rollout["aligned"] is True and rollout["misaligned"] == []
    assert autonomy.load_policy(autonomy.CORE_PROJECT_ID)["mode"] == "canary"
    assert autonomy.load_policy("p1")["mode"] == "canary"

    autonomy.save_policy("p1", {"mode": "degraded"})
    downgraded = autonomy.rollout_status()
    assert downgraded["aligned"] is True and downgraded["downgraded"] == ["p1"]
    assert autonomy.admission_decision(autonomy.CORE_PROJECT_ID)["allowed"] is True


def test_rollout_misalignment_trips_global_brake():
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.ensure_policy("p1")
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"stage": 3})
    autonomy.save_policy("p1", {"stage": 3})
    autonomy.set_platform_mode([autonomy.CORE_PROJECT_ID, "p1"], "canary")
    autonomy.save_policy("p1", {"mode": "shadow"})
    assert autonomy.admission_decision(autonomy.CORE_PROJECT_ID)["allowed"] is False
    assert autonomy.brake_status()["global"]["reason"] == "platform_rollout_misaligned"


def test_rollout_write_failure_rolls_back_and_brakes(monkeypatch):
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.ensure_policy("p1")
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"stage": 3})
    autonomy.save_policy("p1", {"stage": 3})
    real_save = autonomy.save_policy

    def fail_second(project_id, *args, **kwargs):
        if project_id == autonomy.CORE_PROJECT_ID:
            raise OSError("disk full")
        return real_save(project_id, *args, **kwargs)

    monkeypatch.setattr(autonomy, "save_policy", fail_second)
    with pytest.raises(OSError, match="disk full"):
        autonomy.set_platform_mode([autonomy.CORE_PROJECT_ID, "p1"], "canary")
    assert autonomy.load_policy(autonomy.CORE_PROJECT_ID)["mode"] == "shadow"
    assert autonomy.load_policy("p1")["mode"] == "shadow"
    assert autonomy.rollout_status()["state"] == "failed"
    assert autonomy.brake_status()["global"]["active"] is True


def test_full_rollout_requires_formally_proven_stage4():
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    with pytest.raises(autonomy.PolicyError, match="正式達成 Stage 4"):
        autonomy.set_platform_mode([autonomy.CORE_PROJECT_ID], "full")


def test_audit_write_failure_blocks_decision(monkeypatch):
    autonomy.ensure_policy("p1")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(autonomy.secure_write, "secure_write_root", boom)
    with pytest.raises(autonomy.AuditWriteError, match="audit"):
        autonomy.evaluate_operation("p1", "planning", {"risk": "medium"})


def test_weekly_improvements_enqueue_at_most_three_and_are_idempotent(tmp_path):
    report = autonomy.write_weekly_improvements(now=0)
    assert 1 <= len(report["items"]) <= 3
    assert autonomy.verify_weekly_report(report)
    tasks = [t for t in backlog.list_tasks() if t.get("source") == "autonomy_weekly"]
    assert len(tasks) == len(report["enqueued_task_ids"])
    assert all("驗收標準：" in task["detail"] and task["eligible"] is True for task in tasks)

    again = autonomy.write_weekly_improvements(now=0)
    assert again == report
    assert len(backlog.list_tasks()) == len(tasks)

    path = tmp_path / "ap" / "autonomy" / "weekly-improvements" / "1970-W01.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["items"][0]["acceptance"] = "偽造成功"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(autonomy.AuditWriteError, match="完整性"):
        autonomy.write_weekly_improvements(now=0)


def test_weekly_improvements_need_actionable_evidence_not_unknown_or_aggregate():
    base = {
        "eligible": 0,
        "completion_rate": None,
        "failures_by_outcome": {},
        "interventions": {
            "by_type": {"ops_rescue": 8, "bug_design_fix": 3},
            "by_path": {
                "ops_rescue:pause": 4,
                "bug_design_fix:task_action": 3,
                "ops_rescue:baseline_drift_recovery": 1,
            },
        },
        "alerts": {"red_drills_complete": True},
        "rollback": {"success_rate": 1.0},
        "cost": {"unknown_runs": 0},
    }
    assert autonomy.weekly_improvements(base) == []

    base["eligible"] = 3
    base["completion_rate"] = 0.5
    # Incomplete runs without a terminal failure category still must not create
    # a synthetic "unknown" implementation task.
    assert autonomy.weekly_improvements(base) == []


def test_weekly_improvements_name_repeated_failure_and_intervention_paths():
    metrics = {
        "eligible": 5,
        "completion_rate": 0.6,
        "failures_by_outcome": {"deploy_failed": 2, "blocked": 1},
        "interventions": {
            "by_type": {"ops_rescue": 2},
            "by_path": {"ops_rescue:deploy_health_recovery": 2},
        },
        "alerts": {"red_drills_complete": True},
        "rollback": {"success_rate": 1.0},
        "cost": {"unknown_runs": 0},
    }
    items = autonomy.weekly_improvements(metrics)
    assert items[0]["title"].endswith("deploy_failed）")
    assert items[1]["title"] == "消除重複人工介入：ops_rescue/deploy_health_recovery"
    assert "ops_rescue:deploy_health_recovery" in items[1]["acceptance"]


def test_weekly_improvements_reject_meta_and_duplicate_work(monkeypatch):
    backlog.add("已存在的實作工作", source="manual")
    monkeypatch.setattr(
        autonomy,
        "weekly_improvements",
        lambda metrics: [
            {"title": "盤點現有能力", "acceptance": "列出結果", "priority": 1},
            {"title": "沒有驗收", "acceptance": "", "priority": 1},
            {"title": "已存在的實作工作", "acceptance": "測試綠燈", "priority": 1},
            {"title": "修復可重現的部署失敗", "acceptance": "故障注入連續三次通過", "priority": 0},
        ],
    )
    report = autonomy.write_weekly_improvements(now=604800)
    assert [item["title"] for item in report["items"]] == ["修復可重現的部署失敗"]
    assert {item["reason"] for item in report["skipped"]} == {
        "meta_work_forbidden",
        "missing_acceptance",
        "similar_existing_work",
    }


def test_formal_stage_promotion_requires_green_snapshot_and_starts_stage4_afterward(monkeypatch):
    ids = [autonomy.CORE_PROJECT_ID, "p1"]
    for pid in ids:
        autonomy.ensure_policy(pid)
        autonomy.save_policy(pid, {"stage": 3})

    fake_metrics = {
        "promotion": {
            "stage3": {"ready": True, "checks": {"continuous_28_days": True}},
            "stage4": {"ready": False, "checks": {"continuous_28_days_after_stage3": False}},
        }
    }
    monkeypatch.setattr(autonomy, "maturity_metrics", lambda *args, **kwargs: fake_metrics)
    assert autonomy._stage4_start_ts(ids) is None
    promoted = autonomy.promote_stage(ids, 3)
    report_path = next(
        (config.AUTOPILOT_STATE_DIR / "autonomy" / "promotion-reports").glob("*.json")
    )
    assert promoted["changed"] is True
    assert autonomy.verify_promotion_report(json.loads(report_path.read_text(encoding="utf-8")))
    assert all(autonomy.official_stage(pid) == 3 for pid in ids)
    assert autonomy._stage4_start_ts(ids) is not None
    assert autonomy.promote_stage(ids, 3)["changed"] is False
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["metrics"] = {"forged": True}
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert autonomy.promotion_evidence_status()["valid"] is False
    assert all(autonomy.official_stage(pid) == 2 for pid in ids)
    assert autonomy._stage4_start_ts(ids) is None


def test_stage4_cannot_skip_formal_stage3_even_if_policy_target_is_four():
    ids = [autonomy.CORE_PROJECT_ID, "p1"]
    for pid in ids:
        autonomy.ensure_policy(pid)
        autonomy.save_policy(
            pid,
            {
                "stage": 4,
                "intent": {
                    "north_star": "healthy",
                    "success_metrics": ["healthy=1"],
                    "forbidden_actions": ["no destructive changes"],
                },
            },
        )
    with pytest.raises(autonomy.PolicyError, match="不得從 Stage 2 跳升"):
        autonomy.promote_stage(ids, 4)


def test_stage4_streak_excludes_day_that_began_before_stage3_promotion(tmp_path):
    reports = config.AUTOPILOT_STATE_DIR / "autonomy" / "maturity-reports"
    reports.mkdir(parents=True)
    promotion_ts = 38 * 86400 + 43200
    previous_hash = None
    for day_start in (38 * 86400, 39 * 86400):
        report = {
            "day": time.strftime("%Y-%m-%d", time.gmtime(day_start)),
            "generated_at": day_start + 86400 + 3600,
            "period_start": day_start,
            "period_end": day_start + 86400,
            "previous_report_hash": previous_hash or "unknown",
            "stage4_daily_green": True,
        }
        canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        report["report_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        (reports / f"{report['day']}.json").write_text(json.dumps(report), encoding="utf-8")
        previous_hash = report["report_hash"]

    assert (
        autonomy._report_streak(
            now=40 * 86400 + 3600,
            not_before=promotion_ts,
            green_field="stage4_daily_green",
        )
        == 1
    )


@pytest.mark.asyncio
async def test_shadow_policy_blocks_deploy_before_git_mutation(monkeypatch):
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    touched = []

    async def should_not_run(*args, **kwargs):
        touched.append(args)
        return 0, ""

    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(deploy, "_run", should_not_run)
    ok, message = await deploy.redeploy()
    assert ok is False and "shadow" in message
    assert touched == []


def _age_active_brakes(seconds: float = 86_400.0) -> None:
    """把現存煞車的 tripped_at 往回撥，模擬跨 UTC 日後的殘留條目。"""
    path = autonomy._brakes_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    aged = time.time() - seconds
    if data.get("global"):
        data["global"]["tripped_at"] = aged
    for entry in (data.get("projects") or {}).values():
        entry["tripped_at"] = aged
    path.write_text(json.dumps(data), encoding="utf-8")


def test_daily_budget_brakes_expire_at_utc_rollover():
    """每日額度煞車跨日必須自動過期，否則計數歸零後仍永久擋 admission。"""
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    autonomy.trip_brake("global", "platform_daily_pr_budget_exceeded:20", project_id="p1")
    autonomy.trip_brake("project", "daily_pr_budget_exceeded:20", project_id="p1")
    assert autonomy.admission_decision("p1")["allowed"] is False

    _age_active_brakes()
    admission = autonomy.admission_decision("p1")
    assert admission["allowed"] is True
    brakes = autonomy.brake_status()
    assert not (brakes.get("global") or {}).get("active")
    assert not (brakes.get("projects") or {}).get("p1", {}).get("active")


def test_daily_budget_brake_same_day_stays_active():
    """當日觸發的每日額度煞車不得提前解除——過期只認 UTC 跨日。"""
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    autonomy.trip_brake("project", "project_daily_cost_budget_exceeded:6.0", project_id="p1")
    admission = autonomy.admission_decision("p1")
    assert admission["allowed"] is False
    assert admission["reasons"] == ["project_brake:project_daily_cost_budget_exceeded:6.0"]


def test_incident_brakes_survive_utc_rollover():
    """事故型煞車(連敗/回滾/漂移)跨日仍 durable,只認管理員 clear。"""
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary"})
    autonomy.trip_brake("project", "consecutive_failures:3", project_id="p1")
    autonomy.trip_brake("global", "platform_rollout_misaligned", project_id="p1")
    _age_active_brakes()
    admission = autonomy.admission_decision("p1")
    assert admission["allowed"] is False
    assert set(admission["reasons"]) == {
        "global_brake:platform_rollout_misaligned",
        "project_brake:consecutive_failures:3",
    }


def test_daily_budget_brake_retrips_when_still_over_after_rollover():
    """跨日過期後若「本日」原始事件仍超額,admission 重算必須立刻重觸發。"""
    autonomy.ensure_policy("p1")
    autonomy.save_policy("p1", {"mode": "canary", "limits": {"daily_cost_usd": 5}})
    autonomy.emit_event(
        "autonomy_decision",
        run_id="r1",
        project_id="p1",
        eligible=True,
        cost_usd=6.0,
        outcome="failed",
    )
    assert autonomy.admission_decision("p1")["allowed"] is False
    _age_active_brakes()
    admission = autonomy.admission_decision("p1")
    assert admission["allowed"] is False
    assert any("project_daily_cost_budget_exceeded" in r for r in admission["reasons"])
