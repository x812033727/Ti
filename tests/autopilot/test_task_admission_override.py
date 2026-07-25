"""一次性、scope-bound admission quality override。"""

from __future__ import annotations

import pytest

from studio import backlog, config
from studio.task_admission import apply_override, claim_next_task, enqueue_task


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state_dir)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return {"repo": repo, "state": state_dir}


def _context(state, sha: str = "a" * 40) -> dict:
    return {"root": state["repo"], "repo_sha": sha}


def _parked_quality_task(state):
    return enqueue_task(
        "模糊人工任務",
        source="manual",
        risk="low",
        mode="enforce",
        repo_context=_context(state),
    )


def test_override_is_atomic_one_time_and_consumed_on_claim(state):
    task = _parked_quality_task(state)
    assert task["status"] == "parked"
    scope_hash = task["admission"]["scope_hash"]

    stale, error = apply_override(
        task["id"],
        "0" * 64,
        "接受目前低風險預設",
        repo_context=_context(state),
    )
    assert stale is None and error == "stale_scope"

    overridden, error = apply_override(
        task["id"],
        scope_hash,
        "接受目前低風險預設",
        repo_context=_context(state),
    )
    assert error == ""
    assert overridden["status"] == "pending"
    assert overridden["attempts"] == 0
    assert overridden["admission"]["outcome"] == "ready"
    assert overridden["admission"]["original_outcome"] == "needs_clarification"
    assert overridden["admission"]["override"]["reason"] == "接受目前低風險預設"
    assert overridden.get("human_approved") is not True

    repeated, error = apply_override(
        task["id"],
        scope_hash,
        "再覆寫一次",
        repo_context=_context(state),
    )
    assert repeated is None and error == "already_overridden"

    selected = claim_next_task(mode="enforce", repo_context=_context(state))
    assert selected.task["id"] == task["id"]
    current = backlog.get(task["id"])
    assert current["status"] == "in_progress"
    assert current["attempts"] == 1
    assert current["admission"]["override"]["consumed_at"] > 0


def test_override_invalidates_when_repo_sha_changes(state):
    task = _parked_quality_task(state)
    overridden, error = apply_override(
        task["id"],
        task["admission"]["scope_hash"],
        "接受目前低風險預設",
        repo_context=_context(state),
    )
    assert overridden and not error

    selected = claim_next_task(
        mode="enforce",
        repo_context=_context(state, sha="b" * 40),
    )

    assert selected is None
    current = backlog.get(task["id"])
    assert current["status"] == "parked"
    assert current["admission"]["outcome"] == "needs_clarification"
    assert "override" not in current["admission"]


def test_parked_override_refreshes_stale_scope_after_repo_sha_changes(state):
    task = _parked_quality_task(state)
    old_scope = task["admission"]["scope_hash"]

    refreshed, error = apply_override(
        task["id"],
        old_scope,
        "先刷新到新版本，不可沿用舊核准",
        repo_context=_context(state, sha="b" * 40),
    )

    assert refreshed is None
    assert error == "stale_scope_refreshed"
    current = backlog.get(task["id"])
    assert current["status"] == "parked"
    assert current["admission"]["scope_hash"] != old_scope
    assert current["admission"]["audit"]["repo_sha"] == "b" * 40

    overridden, error = apply_override(
        task["id"],
        current["admission"]["scope_hash"],
        "已重新確認新版本範圍",
        repo_context=_context(state, sha="b" * 40),
    )
    assert error == ""
    assert overridden["status"] == "pending"
    assert overridden["admission"]["outcome"] == "ready"


def test_override_invalidates_when_task_semantics_change(state):
    task = _parked_quality_task(state)
    overridden, error = apply_override(
        task["id"],
        task["admission"]["scope_hash"],
        "接受目前低風險預設",
        repo_context=_context(state),
    )
    assert overridden and not error

    backlog.set_status(
        task["id"],
        "pending",
        title="模糊人工任務（需求內容已變更）",
    )
    selected = claim_next_task(mode="enforce", repo_context=_context(state))

    assert selected is None
    current = backlog.get(task["id"])
    assert current["status"] == "parked"
    assert current["admission"]["outcome"] == "needs_clarification"
    assert "override" not in current["admission"]


def test_override_rejects_unknown_repo_sha(state):
    task = enqueue_task(
        "缺少 repo SHA 的模糊人工任務",
        source="manual",
        risk="low",
        mode="enforce",
        repo_context={"root": state["repo"]},
    )

    result, error = apply_override(
        task["id"],
        task["admission"]["scope_hash"],
        "無法證明 scope 未變時不應放行",
        repo_context={"root": state["repo"]},
    )

    assert result is None
    assert error == "unknown_repo_sha"


def test_safety_block_cannot_be_overridden(state):
    task = enqueue_task(
        "寫入外部服務",
        source="manual",
        risk="medium",
        mode="enforce",
        repo_context={
            **_context(state),
            "known_targets": ["service:external"],
        },
        contract={
            "version": 1,
            "outcome": "外部服務已更新",
            "kind": "ops",
            "targets": ["service:external"],
            "acceptance": ["dry-run health check", "rollback 演練"],
            "external_writes": ["external:update"],
        },
    )
    assert task["admission"]["outcome"] == "blocked"
    assert task["admission"]["overridable"] is False

    result, error = apply_override(
        task["id"],
        task["admission"]["scope_hash"],
        "試圖繞過安全治理",
        repo_context={
            **_context(state),
            "known_targets": ["service:external"],
        },
    )

    assert result is None
    assert error == "not_overridable"
    assert backlog.get(task["id"])["status"] == "parked"


def test_unknown_external_uri_scheme_cannot_be_disguised_as_quality_gap(state):
    task = enqueue_task(
        "Sync backup artifact",
        source="manual",
        risk="medium",
        mode="enforce",
        repo_context=_context(state),
        contract={
            "version": 1,
            "outcome": "production backup contains the artifact",
            "kind": "ops",
            "targets": ["s3:production-backup"],
            "acceptance": ["dry-run health check", "rollback drill"],
            "external_writes": [],
        },
    )

    assert task["admission"]["outcome"] == "blocked"
    assert task["admission"]["overridable"] is False
    assert task["admission"]["reasons"] == ["external_write_not_declared"]
    result, error = apply_override(
        task["id"],
        task["admission"]["scope_hash"],
        "試圖把外部 URI 當成不存在的本機路徑",
        repo_context=_context(state),
    )
    assert result is None
    assert error == "not_overridable"
