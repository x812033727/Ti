"""QA verification for task #1 deploy follow-ups.

These tests pin the behavior that is easy to accidentally fake-green:
- working clone fetch must use an exact force refspec, including slashy branch names.
- a failed fetch must stop clone preparation instead of resetting to stale refs.
- redeploy tests can isolate import_smoke without spawning the real import subprocess.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from studio import autopilot as autopilot_mod, redeploy, runner


def _force_refspec(branch: str) -> str:
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


@pytest.mark.asyncio
async def test_prepare_clone_fetches_exact_force_refspec_for_slashy_branch(tmp_path, monkeypatch):
    branch = "release/2026.10/hotfix"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot_mod.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot_mod.config, "GITHUB_TOKEN", "")

    async def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return 0, "ok"

    monkeypatch.setattr(autopilot_mod, "_run", fake_run)

    assert await autopilot_mod._prepare_clone(str(work)) == str(work)

    fetches = [cmd for cmd in calls if cmd[:3] == ["git", "fetch", "origin"]]
    assert fetches == [["git", "fetch", "origin", _force_refspec(branch)]]
    assert ["git", "fetch", "origin", branch] not in calls


@pytest.mark.asyncio
async def test_prepare_clone_fetch_failure_is_not_ignored(tmp_path, monkeypatch):
    branch = "deploy/test"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot_mod.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot_mod.config, "GITHUB_TOKEN", "")

    async def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 128, "fatal: cannot lock ref"
        return 0, "ok"

    monkeypatch.setattr(autopilot_mod, "_run", fake_run)

    with pytest.raises(RuntimeError, match="fetch|clone|工作"):
        await autopilot_mod._prepare_clone(str(work))

    assert ["git", "reset", "--hard", f"origin/{branch}"] not in calls


@pytest.mark.asyncio
async def test_redeploy_restart_path_mocks_import_smoke_without_real_subprocess(
    monkeypatch,
):
    run_calls: list[list[str]] = []
    smoke_called = False
    restart_called = False

    @contextmanager
    def fake_deploy_lock():
        yield True

    async def fake_run_command_exec(_cwd, argv, **_kwargs):
        run_calls.append(list(argv))
        if argv == ["git", "pull", "--ff-only"]:
            return runner.RunOutput("git pull", 0, "Already up to date.", False)
        raise AssertionError(f"unexpected real subprocess command: {argv!r}")

    async def fake_import_smoke():
        nonlocal smoke_called
        smoke_called = True
        return runner.RunOutput("import smoke", 0, "ok", False)

    def fake_schedule_restart():
        nonlocal restart_called
        restart_called = True

    monkeypatch.setattr(redeploy.autonomy, "policy_exists", lambda _project_id: False)
    monkeypatch.setattr(redeploy.deploy, "_deploy_lock", fake_deploy_lock)
    monkeypatch.setattr(runner, "run_command_exec", fake_run_command_exec)
    monkeypatch.setattr(redeploy, "import_smoke", fake_import_smoke)
    monkeypatch.setattr(redeploy, "schedule_restart", fake_schedule_restart)

    result = await redeploy.redeploy()

    assert result["ok"] is True
    assert result["restarting"] is True
    assert smoke_called is True
    assert restart_called is True
    assert run_calls == [["git", "pull", "--ff-only"]]
