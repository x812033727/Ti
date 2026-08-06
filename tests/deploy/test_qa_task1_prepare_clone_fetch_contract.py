"""QA guard for task #1: working clone fetch must avoid bare branch updates."""

from __future__ import annotations

import pytest

from studio import autopilot


@pytest.mark.asyncio
async def test_prepare_clone_fetch_uses_force_refspec_with_expected_runtime_options(
    tmp_path, monkeypatch
):
    branch = "feature/cas-race"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    auth_env = {"GIT_ASKPASS": "qa-helper"}
    calls: list[dict] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_git_cred_argv", lambda: [])
    monkeypatch.setattr(autopilot, "_git_cred_env", lambda: auth_env)

    async def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 0, ""
        if cmd in (
            ["git", "reset", "--hard", "HEAD"],
            ["git", "clean", "-fdq"],
            ["git", "checkout", "-q", branch],
            ["git", "reset", "--hard", f"origin/{branch}"],
            ["git", "config", "user.email", "noreply@anthropic.com"],
            ["git", "config", "user.name", "Ti Autopilot"],
        ):
            return 0, ""
        raise AssertionError(f"unexpected git command: {cmd!r}")

    monkeypatch.setattr(autopilot, "_run", fake_run)

    assert await autopilot._prepare_clone(str(work)) == str(work)

    fetch_calls = [call for call in calls if call["cmd"][:3] == ["git", "fetch", "origin"]]
    assert len(fetch_calls) == 1
    fetch = fetch_calls[0]
    assert fetch["cmd"] == [
        "git",
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    ]
    assert fetch["cmd"] != ["git", "fetch", "origin", branch]
    assert fetch["cwd"] == str(work)
    assert fetch["timeout"] == 120
    assert fetch["env"] == auth_env


@pytest.mark.asyncio
async def test_prepare_clone_rejects_invalid_repo_sha_before_any_git_command(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(autopilot, "_run", fake_run)

    with pytest.raises(RuntimeError, match="repo SHA 格式錯誤"):
        await autopilot._prepare_clone(str(tmp_path / "work"), repo_sha="not-a-sha")

    assert calls == []
