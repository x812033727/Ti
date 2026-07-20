"""ProjectImprover 以最終實際 diff 做 shadow/雙 provider 發佈治理。"""

from __future__ import annotations

import hashlib

import pytest

from studio import autonomy, backlog, config, improver, projects, publisher, repo_base, runner


async def _noop(_event):
    return None


@pytest.fixture
def governance_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(config, "PUBLISH_MERGE", True)
    monkeypatch.setattr(config, "FAST_LANE", False)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)

    async def sync(*args, **kwargs):
        return repo_base.SyncResult("skipped", "test")

    monkeypatch.setattr(repo_base, "ensure_base", sync)
    monkeypatch.setattr(runner, "git_current_branch", _main_branch)
    return tmp_path


async def _main_branch(_cwd):
    return "main"


class _GuardCallingSession:
    seen_kwargs: dict = {}

    def __init__(self, sid, broadcast, **kwargs):
        self.session_id = sid
        self.guard = kwargs.get("publish_guard")
        _GuardCallingSession.seen_kwargs = kwargs

    def request_stop(self):
        return None

    async def run(self, requirement):
        assert self.guard is not None
        allowed, detail = await self.guard(
            "initial",
            {
                "attempt": "initial",
                "shippable": True,
                "all_tasks_passed": True,
                "demo": {"exit_code": 0, "timed_out": False},
                "tasks": [{"title": "change", "status": "done"}],
                "commit": "new-commit",
            },
        )
        return {
            "completed": True,
            "shippable": True,
            "followups": [],
            "publish_result": {"ok": allowed, "detail": detail, "merged": False},
        }


@pytest.mark.asyncio
async def test_managed_shadow_reviews_actual_diff_and_parks_without_publish(
    governance_env, monkeypatch
):
    project = projects.create("納管專案", vision="safe")
    assert project is not None
    sdir = projects.state_dir(project["id"])
    task = backlog.add(
        "high risk reversible change",
        state_dir=sdir,
        risk="high-reversible",
        rollback={"dry_run": True, "backup": True, "verified": True, "scope_limit": "one"},
    )
    assert task is not None
    diff = "diff --git a/app.py b/app.py\n+safe = True\n"
    command_calls = []

    async def command(_cwd, argv, **kwargs):
        command_calls.append(tuple(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return runner.RunOutput("git head", 0, "a" * 40 + "\n", False)
        if argv[:2] == ["git", "status"]:
            return runner.RunOutput("git status", 0, "", False)
        assert argv[:3] == ["git", "diff", "--binary"]
        return runner.RunOutput("autonomy publish diff", 0, diff, False)

    review_calls = []

    async def review(**kwargs):
        review_calls.append(kwargs)
        return [
            {
                "provider": provider,
                "verdict": "approve",
                "rationale": "bounded and reversible",
                "diff_sha": kwargs["diff_sha"],
                "evidence_sha": kwargs["evidence_sha"],
            }
            for provider in ("claude", "codex")
        ]

    monkeypatch.setattr(runner, "run_command_exec", command)
    monkeypatch.setattr(improver.autonomy_review, "review", review)
    monkeypatch.setattr(improver, "StudioSession", _GuardCallingSession)

    completed = await improver.ProjectImprover(project, _noop)._run_task(task, sdir)

    assert completed is True  # shadow 產出有效決策證據，不計執行失敗
    stored = backlog.list_tasks(state_dir=sdir)[0]
    assert stored["status"] == "parked"
    assert "shadow" in stored["note"]
    assert stored["diff_sha"] == hashlib.sha256(diff.encode()).hexdigest()
    assert review_calls and review_calls[0]["diff_text"] == diff
    assert review_calls[0]["diff_sha"] == stored["diff_sha"]
    assert [row for row in command_calls if row[:3] == ("git", "diff", "--binary")]
    assert _GuardCallingSession.seen_kwargs["auto_publish"] is True


@pytest.mark.asyncio
async def test_unmanaged_legacy_project_keeps_publish_path(governance_env, monkeypatch):
    """舊專案沒有 policy 時不偷偷建檔，也不安裝新 guard。"""
    pid = "legacy123"
    pdir = config.PROJECTS_ROOT / pid
    pdir.mkdir(parents=True)
    project = {
        "id": pid,
        "name": "legacy",
        "vision": "",
        "publish_repo": "",
    }
    task = backlog.add("legacy change", state_dir=pdir, risk="low")
    assert task is not None and not autonomy.policy_exists(pid)

    async def command(_cwd, argv, **kwargs):
        if argv[:2] == ["git", "status"]:
            return runner.RunOutput("git status", 0, "", False)
        return runner.RunOutput("git head", 0, "b" * 40 + "\n", False)

    class LegacySession:
        seen = None

        def __init__(self, sid, broadcast, **kwargs):
            LegacySession.seen = kwargs

        async def run(self, requirement):
            return {"completed": True, "shippable": True, "followups": []}

        def request_stop(self):
            return None

    monkeypatch.setattr(runner, "run_command_exec", command)
    monkeypatch.setattr(improver, "StudioSession", LegacySession)
    completed = await improver.ProjectImprover(project, _noop)._run_task(task, pdir)

    assert completed is True
    assert LegacySession.seen["publish_guard"] is None
    assert not autonomy.policy_exists(pid)
    assert backlog.list_tasks(state_dir=pdir)[0]["status"] == "done"


@pytest.mark.asyncio
async def test_stage4_records_healthy_deployed_only_after_revision_probe(
    governance_env, monkeypatch
):
    project = projects.create("stage4 product", vision="healthy")
    assert project is not None
    projects.set_publish_repo(project["id"], "owner/product")
    project = projects.get(project["id"])
    assert project is not None
    autonomy.save_policy(
        project["id"],
        {
            "mode": "canary",
            "stage": 4,
            "intent": {
                "north_star": "healthy deploy",
                "success_metrics": ["healthy=1"],
                "forbidden_actions": ["no destructive migration"],
            },
            "deployment": {
                "health_url": "https://product.example/healthz",
                "healthy_field": "ok",
                "revision_field": "build.git_sha",
                "timeout_s": 10,
                "poll_interval_s": 10,
            },
        },
    )
    sdir = projects.state_dir(project["id"])
    task = backlog.add(
        "deploy reversible change",
        state_dir=sdir,
        risk="high-reversible",
        rollback={"dry_run": True, "backup": True, "verified": True, "scope_limit": "one"},
    )
    assert task is not None
    source_sha, merge_sha = "a" * 40, "c" * 40

    async def command(_cwd, argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return runner.RunOutput("git head", 0, source_sha + "\n", False)
        if argv[:2] == ["git", "status"]:
            return runner.RunOutput("git status", 0, "", False)
        return runner.RunOutput("autonomy publish diff", 0, "diff --git a/x b/x\n+x\n", False)

    async def review(**kwargs):
        return [
            {
                "provider": provider,
                "verdict": "approve",
                "rationale": "bounded and reversible",
                "diff_sha": kwargs["diff_sha"],
                "evidence_sha": kwargs["evidence_sha"],
            }
            for provider in ("claude", "codex")
        ]

    health_calls = []

    async def health(contract, expected):
        health_calls.append((contract, expected))
        return True, "health_and_revision_verified"

    async def base_head(_repo, _base):
        return source_sha

    class MergedSession:
        def __init__(self, sid, broadcast, **kwargs):
            self.guard = kwargs["publish_guard"]
            self.verifier = kwargs["post_publish_verifier"]

        def request_stop(self):
            return None

        async def run(self, requirement):
            allowed, _ = await self.guard(
                "initial",
                {
                    "attempt": "initial",
                    "shippable": True,
                    "all_tasks_passed": True,
                    "demo": {"exit_code": 0, "timed_out": False},
                    "tasks": [{"title": "change", "status": "done"}],
                    "commit": "new-commit",
                },
            )
            assert allowed is True
            healthy, detail = await self.verifier({"merge_sha": merge_sha, "merged": True})
            return {
                "completed": True,
                "shippable": True,
                "followups": [],
                "publish_result": {
                    "ok": healthy,
                    "detail": detail,
                    "merged": True,
                    "merge_sha": merge_sha,
                    "health_verified": healthy,
                },
            }

    monkeypatch.setattr(runner, "run_command_exec", command)
    monkeypatch.setattr(improver.autonomy_review, "review", review)
    monkeypatch.setattr(improver.project_health, "verify", health)
    monkeypatch.setattr(improver.publisher, "base_head_sha", base_head)
    monkeypatch.setattr(improver, "StudioSession", MergedSession)

    completed = await improver.ProjectImprover(project, _noop)._run_task(task, sdir)

    assert completed is True
    assert backlog.list_tasks(state_dir=sdir)[0]["status"] == "done"
    assert [expected for _contract, expected in health_calls] == [
        source_sha,
        source_sha,
        merge_sha,
    ]
    terminal = [
        event
        for event in autonomy.read_events(1)
        if event.get("run_id", "").startswith("pj") and event.get("outcome") == "healthy_deployed"
    ]
    assert len(terminal) == 1


@pytest.mark.asyncio
async def test_unhealthy_external_deploy_executes_and_verifies_exact_rollback(
    governance_env, monkeypatch
):
    project = projects.create("rollback product", vision="safe deploy")
    assert project is not None
    projects.set_publish_repo(project["id"], "owner/product")
    project = projects.get(project["id"])
    autonomy.save_policy(
        project["id"],
        {
            "mode": "canary",
            "stage": 4,
            "intent": {
                "north_star": "healthy deploy",
                "success_metrics": ["healthy=1"],
                "forbidden_actions": ["no destructive migration"],
            },
            "deployment": {
                "health_url": "https://product.example/healthz",
                "healthy_field": "ok",
                "revision_field": "build.git_sha",
                "timeout_s": 10,
                "poll_interval_s": 10,
            },
        },
    )
    sdir = projects.state_dir(project["id"])
    task = backlog.add(
        "deploy reversible change",
        state_dir=sdir,
        risk="high-reversible",
        rollback={"dry_run": True, "backup": True, "verified": True, "scope_limit": "one"},
    )
    assert task is not None
    source_sha, bad_sha, rollback_sha = "a" * 40, "b" * 40, "c" * 40

    async def command(_cwd, argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return runner.RunOutput("git head", 0, source_sha + "\n", False)
        if argv[:2] == ["git", "status"]:
            return runner.RunOutput("git status", 0, "", False)
        return runner.RunOutput("autonomy publish diff", 0, "diff --git a/x b/x\n+x\n", False)

    async def review(**kwargs):
        return [
            {
                "provider": provider,
                "verdict": "approve",
                "rationale": "bounded and reversible",
                "diff_sha": kwargs["diff_sha"],
                "evidence_sha": kwargs["evidence_sha"],
            }
            for provider in ("claude", "codex")
        ]

    health_revisions = []

    async def health(_contract, expected):
        health_revisions.append(expected)
        if expected == bad_sha:
            return False, "deployment_health_timeout:unhealthy_response"
        return True, "health_and_revision_verified"

    async def base_head(_repo, _base):
        return source_sha

    rollback_calls = []

    async def rollback(_cwd, session_id, **kwargs):
        rollback_calls.append((session_id, kwargs))
        return publisher.PublishResult(
            True,
            "rollback PR 已通過 CI 並合併",
            pr_number=88,
            merged=True,
            merge_sha=rollback_sha,
        )

    class UnhealthySession:
        def __init__(self, sid, broadcast, **kwargs):
            self.guard = kwargs["publish_guard"]
            self.verifier = kwargs["post_publish_verifier"]

        def request_stop(self):
            return None

        async def run(self, requirement):
            allowed, _ = await self.guard(
                "initial",
                {
                    "attempt": "initial",
                    "shippable": True,
                    "all_tasks_passed": True,
                    "demo": {"exit_code": 0, "timed_out": False},
                    "tasks": [{"title": "change", "status": "done"}],
                    "commit": "new-commit",
                },
            )
            assert allowed is True
            healthy, detail = await self.verifier({"merge_sha": bad_sha, "merged": True})
            return {
                "completed": True,
                "shippable": True,
                "followups": [],
                "publish_result": {
                    "ok": healthy,
                    "detail": detail,
                    "merged": True,
                    "merge_sha": bad_sha,
                    "health_verified": healthy,
                },
            }

    monkeypatch.setattr(runner, "run_command_exec", command)
    monkeypatch.setattr(improver.autonomy_review, "review", review)
    monkeypatch.setattr(improver.project_health, "verify", health)
    monkeypatch.setattr(improver.publisher, "base_head_sha", base_head)
    monkeypatch.setattr(improver.publisher, "rollback_merge", rollback)
    monkeypatch.setattr(improver, "StudioSession", UnhealthySession)

    completed = await improver.ProjectImprover(project, _noop)._run_task(task, sdir)

    assert completed is False
    assert health_revisions == [source_sha, source_sha, bad_sha, rollback_sha]
    assert rollback_calls[0][1]["bad_merge_sha"] == bad_sha
    assert rollback_calls[0][1]["previous_sha"] == source_sha
    rollback_events = [
        event for event in autonomy.read_events(1) if event.get("event_type") == "rollback_result"
    ]
    assert len(rollback_events) == 1
    assert rollback_events[0]["outcome"] == "success"
    assert rollback_events[0]["payload"]["pr"] == 88
    assert autonomy.brake_status()["projects"][project["id"]]["active"] is True
