"""QA edge coverage for task #1 deploy hardening."""

from __future__ import annotations

import pytest

from studio import autopilot, config, redeploy, runner


@pytest.mark.asyncio
async def test_redeploy_import_smoke_failure_does_not_restart_and_redacts_token(
    monkeypatch, tmp_path
):
    token = "ghp_task1_secret"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "GITHUB_TOKEN", token)

    async def pull_ok():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    async def smoke_fails():
        return runner.RunOutput("import smoke", 1, f"ImportError leaked {token}", False)

    def must_not_restart(*_args, **_kwargs):
        raise AssertionError("restart must not be scheduled when import smoke fails")

    monkeypatch.setattr(redeploy, "pull_main", pull_ok)
    monkeypatch.setattr(redeploy, "import_smoke", smoke_fails)
    monkeypatch.setattr(redeploy, "schedule_restart", must_not_restart)

    result = await redeploy.redeploy()

    assert result["ok"] is False
    assert result["pulled"] is True
    assert result["restarting"] is False
    assert "import 檢查失敗" in result["detail"]
    assert token not in result["detail"]


@pytest.mark.asyncio
async def test_prepare_clone_fetch_failure_aborts_before_resetting_stale_origin(
    tmp_path, monkeypatch
):
    branch = "deploy/test"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_git_cred_argv", lambda: [])
    monkeypatch.setattr(autopilot, "_git_cred_env", lambda: None)

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 128, "fatal: cannot lock ref 'refs/remotes/origin/deploy/test'"
        if cmd[:2] == ["git", "reset"]:
            return 0, ""
        if cmd[:2] == ["git", "clean"]:
            return 0, ""
        if cmd == ["git", "checkout", "-q", branch]:
            return 0, ""
        if cmd[:2] == ["git", "config"]:
            return 0, ""
        raise AssertionError(f"unexpected command after failed fetch: {cmd!r}")

    monkeypatch.setattr(autopilot, "_run", fake_run)

    error = None
    try:
        await autopilot._prepare_clone(str(work))
    except RuntimeError as exc:
        error = exc

    assert error is not None, f"fetch failure was ignored; calls={calls!r}"
    assert any(word in str(error) for word in ("fetch", "origin", "更新"))

    fetch_idx = calls.index(
        [
            "git",
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ]
    )
    assert not any(cmd[:2] == ["git", "reset"] for cmd in calls[fetch_idx + 1 :])


@pytest.mark.asyncio
async def test_prepare_clone_pinned_sha_fetch_failure_aborts_before_local_sha_probe(
    tmp_path, monkeypatch
):
    branch = "deploy/test"
    repo_sha = "a" * 40
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_git_cred_argv", lambda: [])
    monkeypatch.setattr(autopilot, "_git_cred_env", lambda: None)

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 128, "fatal: remote branch changed while fetching"
        raise AssertionError(f"unexpected command after failed fetch: {cmd!r}")

    monkeypatch.setattr(autopilot, "_run", fake_run)

    with pytest.raises(RuntimeError, match="fetch|origin|更新"):
        await autopilot._prepare_clone(str(work), repo_sha=repo_sha)

    assert calls == [
        [
            "git",
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ]
    ]
