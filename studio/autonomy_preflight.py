"""自治觀察窗 preflight：用可重算事實判斷是否能開始 Stage 3／4 觀察。

預設只讀；``--write`` 才把帶內容 hash 的快照寫入 state。輸出刻意不包含 token、webhook URL
或絕對 workspace 路徑，適合由 API／CI 顯示。這不是升階捷徑：Stage 3/4 的 28 日與樣本門檻
仍由 maturity report 判定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import autonomy, backlog, config, projects, secure_write
from .repo_ident import repo_key

PREFLIGHT_VERSION = "autonomy-preflight-v1"


def _git(path: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _hash(report: dict) -> str:
    body = dict(report)
    body.pop("report_hash", None)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_report(report: dict) -> bool:
    return bool(report.get("report_hash")) and report.get("report_hash") == _hash(report)


def _check(ok: bool, evidence: dict | None = None) -> dict:
    return {"ok": bool(ok), "evidence": evidence or {}}


def collect(
    *,
    project_rows: list[dict] | None = None,
    state_dir: Path | None = None,
    deployed_dir: Path | None = None,
    source_dir: Path | None = None,
    now: float | None = None,
) -> dict:
    """收集 preflight；不寫 state、不呼叫 provider 或外部網路。"""
    ts = time.time() if now is None else float(now)
    rows = projects.list_projects() if project_rows is None else project_rows
    ids = [autonomy.CORE_PROJECT_ID, *(str(row.get("id")) for row in rows if row.get("id"))]
    ids = list(dict.fromkeys(ids))
    root = state_dir or config.AUTOPILOT_STATE_DIR
    deployed = Path(deployed_dir or config.AUTOPILOT_DEPLOY_DIR)
    # Inspect the clone autopilot actually mutates, not the deployed application
    # directory from which this module happens to run.  Keeping the two paths
    # distinct catches in-flight or abandoned source changes even when the
    # deployed SHA itself is clean and healthy.
    source = Path(source_dir or config.AUTOPILOT_WORK_DIR)

    deployed_sha = _git(deployed, "rev-parse", "HEAD")
    source_sha = _git(source, "rev-parse", "HEAD")
    deployed_clean = bool(deployed_sha) and not _git(deployed, "status", "--porcelain")
    source_clean = bool(source_sha) and not _git(source, "status", "--porcelain")
    source_repo = repo_key(_git(source, "remote", "get-url", "origin"))
    source_branch = _git(source, "branch", "--show-current")
    expected_repo = repo_key(config.AUTOPILOT_REPO)
    source_identity_aligned = bool(
        deployed_sha
        and source_sha
        and deployed_sha == source_sha
        and deployed_clean
        and source_clean
        and source_repo
        and source_repo == expected_repo
    )

    policy_rows: dict[str, dict] = {}
    policy_complete = True
    shadow_projects: list[str] = []
    planner_ready: list[str] = []
    slo_ready: list[str] = []
    deployment_ready: list[str] = []
    for pid in ids:
        exists = autonomy.policy_exists(pid, state_dir=state_dir)
        policy = autonomy.load_policy(pid, state_dir=state_dir)
        source_contract = policy.get("source") or {}
        required_source = ("repo", "workspace", "publish_repo", "lane")
        source_complete = exists and all(
            str(source_contract.get(key) or "").strip() for key in required_source
        )
        pr_limit_ready = int(policy["limits"]["daily_pr"]) > 0
        complete = source_complete and pr_limit_ready
        policy_complete = policy_complete and complete
        if exists and policy["mode"] == "shadow":
            shadow_projects.append(pid)
        planner = autonomy.stage4_planner_status(pid, state_dir=state_dir)
        if planner["ready"]:
            planner_ready.append(pid)
        if (
            policy["stage"] >= 4
            and float(policy["limits"]["closed_loop_slo_min"]) > 0
            and int(policy["limits"]["slo_min_eligible"]) > 0
        ):
            slo_ready.append(pid)
        deployment = autonomy.deployment_contract_status(pid, state_dir=state_dir)
        if deployment["ready"]:
            deployment_ready.append(pid)
        policy_rows[pid] = {
            "exists": exists,
            "revision": policy["revision"],
            "stage": policy["stage"],
            "mode": policy["mode"] if exists else "unmanaged",
            "source_contract_complete": source_complete,
            "daily_pr_limit_ready": pr_limit_ready,
            "planner_ready": planner["ready"],
            "planner_blocking_reasons": planner["blocking_reasons"],
            "slo_policy_ready": pid in slo_ready,
            "deployment_contract_ready": deployment["ready"],
            "deployment_blocking_reasons": deployment["blocking_reasons"],
        }

    core_policy = autonomy.load_policy(autonomy.CORE_PROJECT_ID, state_dir=state_dir)
    core_source = core_policy.get("source") or {}
    source_contract_aligned = bool(
        repo_key(str(core_source.get("repo") or "")) == expected_repo
        and repo_key(str(core_source.get("publish_repo") or "")) == expected_repo
        and Path(str(core_source.get("workspace") or "")).resolve()
        == Path(config.AUTOPILOT_WORK_DIR).resolve()
        and str(core_source.get("lane") or "") == "main"
        and source_branch == core_policy.get("base_branch")
    )
    source_aligned = source_identity_aligned and source_contract_aligned

    active: dict[str, int] = {}
    core_active = sum(
        task.get("status") in ("in_progress", "merging")
        for task in backlog.list_tasks(state_dir=root)
    )
    if core_active:
        active[autonomy.CORE_PROJECT_ID] = core_active
    for pid in ids:
        if pid == autonomy.CORE_PROJECT_ID:
            continue
        count = sum(
            task.get("status") in ("in_progress", "merging")
            for task in backlog.list_tasks(state_dir=config.PROJECTS_ROOT / pid)
        )
        if count:
            active[pid] = count

    rollout = autonomy.rollout_status(ids, state_dir=state_dir)
    metrics = autonomy.maturity_metrics(28, state_dir=state_dir, project_ids=ids)
    brakes = autonomy.brake_status(state_dir=state_dir)
    reports_dir = root / "autonomy" / "maturity-reports"
    report_files = sorted(reports_dir.glob("*.json")) if reports_dir.is_dir() else []
    maturity_reports = []
    for path in report_files:
        try:
            maturity_reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            maturity_reports.append({})
    chain_valid = autonomy.verify_report_chain(maturity_reports)
    promotion_evidence = autonomy.promotion_evidence_status(state_dir=state_dir)

    notification_ready = metrics["alerts"]["external_sink_configured"]
    red_drills_ready = metrics["alerts"]["red_drills_complete"]
    rollback_ready = bool(ids) and all(
        (metrics["rollback"]["by_project"].get(pid) or {}).get("verified_drill_success", 0) > 0
        and (metrics["rollback"]["by_project"].get(pid) or {}).get("drill_failed", 0) == 0
        and (metrics["rollback"]["by_project"].get(pid) or {}).get("failed", 0) == 0
        for pid in ids
    )
    no_brakes = not (brakes.get("global") or {}).get("active") and not any(
        row.get("active") for row in (brakes.get("projects") or {}).values()
    )
    shadow_aligned = (
        rollout["configured"]
        and rollout["state"] == "committed"
        and rollout["target_mode"] == "shadow"
        and rollout["aligned"]
        and set(shadow_projects) == set(ids)
    )
    distinct_ready_providers = [
        provider for provider in config.AUTO_DISPATCH_PROVIDERS if config.provider_ready(provider)
    ]

    stage3_checks = {
        "source_baseline_aligned": _check(
            source_aligned,
            {
                "deployed_sha": deployed_sha or "unknown",
                "source_sha": source_sha or "unknown",
                "source_repo": source_repo or "unknown",
                "expected_repo": expected_repo or "unknown",
                "source_branch": source_branch or "unknown",
                "deployed_worktree_clean": deployed_clean,
                "source_worktree_clean": source_clean,
                "source_contract_aligned": source_contract_aligned,
            },
        ),
        "all_tasks_quiescent": _check(not active, {"active_by_project": active}),
        "all_policies_and_sources_complete": _check(policy_complete, {"projects": policy_rows}),
        "all_deployment_baseline_contracts_ready": _check(
            set(deployment_ready) == set(ids),
            {"ready_projects": deployment_ready, "required": ids},
        ),
        "platform_shadow_rollout_aligned": _check(
            shadow_aligned,
            {
                "configured": rollout["configured"],
                "state": rollout["state"],
                "target_mode": rollout["target_mode"],
                "misaligned": rollout["misaligned"],
            },
        ),
        "external_notification_configured": _check(notification_ready),
        "all_red_drills_within_5m": _check(
            red_drills_ready,
            {
                "required": metrics["alerts"]["required_red_drill_kinds"],
                "passed": metrics["alerts"]["passed_red_drill_kinds"],
            },
        ),
        "rollback_drill_succeeded": _check(
            rollback_ready,
            {
                "success": metrics["rollback"]["success"],
                "failed": metrics["rollback"]["failed"],
                "by_project": metrics["rollback"]["by_project"],
            },
        ),
        "maturity_report_chain_valid": _check(chain_valid, {"reports": len(maturity_reports)}),
        "promotion_evidence_valid": _check(
            promotion_evidence["valid"],
            {
                "reports": promotion_evidence["reports"],
                "events": promotion_evidence["events"],
                "invalid_files": promotion_evidence["invalid_files"],
                "event_hashes_without_report": promotion_evidence["event_hashes_without_report"],
            },
        ),
        "no_active_brakes": _check(no_brakes),
    }
    stage3_ready = all(item["ok"] for item in stage3_checks.values())

    stage4_checks = {
        "stage3_promotion_proven": _check(
            metrics["promotion"]["stage3"]["ready"]
            and all(autonomy.official_stage(pid, state_dir=state_dir) >= 3 for pid in ids)
            and promotion_evidence["valid"]
        ),
        "all_versioned_planners_ready": _check(
            set(planner_ready) == set(ids), {"ready_projects": planner_ready, "required": ids}
        ),
        "all_versioned_slo_policies_ready": _check(
            set(slo_ready) == set(ids), {"ready_projects": slo_ready, "required": ids}
        ),
        "two_distinct_providers_ready": _check(
            len(set(distinct_ready_providers)) >= 2,
            {"providers": distinct_ready_providers},
        ),
        "all_deployment_health_contracts_ready": _check(
            set(deployment_ready) == set(ids),
            {"ready_projects": deployment_ready, "required": ids},
        ),
        "platform_canary_or_full_aligned": _check(
            rollout["configured"]
            and rollout["state"] == "committed"
            and rollout["target_mode"] in ("canary", "full")
            and rollout["aligned"],
            {
                "state": rollout["state"],
                "target_mode": rollout["target_mode"],
                "misaligned": rollout["misaligned"],
            },
        ),
    }
    stage4_ready = all(item["ok"] for item in stage4_checks.values())
    report = {
        "schema_version": autonomy.SCHEMA_VERSION,
        "calculation_version": PREFLIGHT_VERSION,
        "generated_at": ts,
        "project_ids": ids,
        "stage3_observation": {
            "ready": stage3_ready,
            "blocking_reasons": [key for key, item in stage3_checks.items() if not item["ok"]],
            "checks": stage3_checks,
        },
        "stage4_observation": {
            "ready": stage4_ready,
            "blocking_reasons": [key for key, item in stage4_checks.items() if not item["ok"]],
            "checks": stage4_checks,
        },
    }
    report["report_hash"] = _hash(report)
    return report


def write_report(**kwargs) -> dict:
    report = collect(**kwargs)
    state_dir = kwargs.get("state_dir") or config.AUTOPILOT_STATE_DIR
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(report["generated_at"]))
    path = (
        Path(state_dir)
        / "autonomy"
        / "preflight-reports"
        / (f"{stamp}-{report['report_hash'][:12]}.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_write.secure_write_root(
        path, (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    )
    autonomy.emit_event(
        "autonomy_decision",
        project_id=autonomy.CORE_PROJECT_ID,
        outcome="preflight_snapshot_created",
        payload={
            "report_hash": report["report_hash"],
            "stage3_ready": report["stage3_observation"]["ready"],
            "stage4_ready": report["stage4_observation"]["ready"],
        },
        state_dir=Path(state_dir),
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Ti autonomy observation-window preflight")
    parser.add_argument("--write", action="store_true", help="write an immutable snapshot")
    args = parser.parse_args()
    report = write_report() if args.write else collect()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["stage3_observation"]["ready"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
