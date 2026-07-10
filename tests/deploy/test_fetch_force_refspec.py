"""部署 fetch 必須用 force refspec，避免並行 fetch 更新 origin/<branch> 時 CAS 競爭。"""

from __future__ import annotations

import pytest

from studio import autodeploy, autopilot, deploy


def _force_fetch(branch: str) -> list[str]:
    return ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]


def _assert_force_fetch_seen(calls: list[list[str]], branch: str) -> None:
    expected = _force_fetch(branch)
    assert expected in calls, f"missing force fetch argv {expected!r}; captured={calls!r}"


def test_old_bare_fetch_black_sample_is_rejected():
    branch = "deploy/test"

    with pytest.raises(AssertionError):
        _assert_force_fetch_seen([["git", "fetch", "origin", branch]], branch)


@pytest.mark.asyncio
async def test_autodeploy_fetch_uses_force_refspec(tmp_path, monkeypatch):
    branch = "deploy/test"
    calls: list[list[str]] = []

    monkeypatch.setattr(autodeploy.config, "AUTOPILOT_DEPLOY_DIR", tmp_path / "deploy")
    monkeypatch.setattr(autodeploy.config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(autodeploy.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autodeploy.deploy, "current_head", lambda _deploy_dir: _async_value("same"))

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 0, ""
        if cmd == ["git", "rev-parse", f"origin/{branch}"]:
            return 0, "same\n"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(autodeploy.deploy, "_run", fake_run)

    assert await autodeploy.run_once() == 0
    _assert_force_fetch_seen(calls, branch)


@pytest.mark.asyncio
async def test_redeploy_fetch_uses_force_refspec(tmp_path, monkeypatch):
    branch = "deploy/test"
    heads = iter(["oldhead", "newhead"])
    calls: list[list[str]] = []

    monkeypatch.setattr(deploy.config, "AUTOPILOT_DEPLOY_DIR", tmp_path / "deploy")
    monkeypatch.setattr(deploy.config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(deploy.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(deploy.config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(deploy, "current_head", lambda _deploy_dir: _async_value(next(heads)))
    monkeypatch.setattr(deploy, "_reinstall_and_restart", lambda *_args: _async_value((True, "ok")))
    monkeypatch.setattr(deploy, "health_check", lambda: _async_value((True, "ok")))

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 0, ""
        if cmd == ["git", "reset", "--hard", f"origin/{branch}"]:
            return 0, ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(deploy, "_run", fake_run)

    ok, _msg = await deploy.redeploy()

    assert ok is True
    _assert_force_fetch_seen(calls, branch)


@pytest.mark.asyncio
async def test_boundary_redeploy_check_fetch_uses_force_refspec(tmp_path, monkeypatch):
    branch = "deploy/test"
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_DEPLOY_DIR", tmp_path / "deploy")
    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot.config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(autopilot.config, "AUTOPILOT_DEPLOY_CHECK_INTERVAL", 1)
    monkeypatch.setattr(autopilot, "_last_deploy_check_at", 0.0)
    monkeypatch.setattr(autopilot, "_deploy_backoff_until", 0.0)
    monkeypatch.setattr(autopilot.deploy, "current_head", lambda _deploy_dir: _async_value("local"))
    monkeypatch.setattr(autopilot.history, "busy_sessions", lambda _stale_after: ["manual-session"])

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 0, ""
        if cmd == ["git", "rev-parse", f"origin/{branch}"]:
            return 0, "remote\n"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(autopilot.deploy, "_run", fake_run)

    await autopilot._maybe_boundary_redeploy()

    _assert_force_fetch_seen(calls, branch)


async def _async_value(value):
    return value
