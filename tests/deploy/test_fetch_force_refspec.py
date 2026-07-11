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
    """bare fetch（無 force refspec）必須被拒絕——判別力黑樣本。"""
    branch = "deploy/test"

    with pytest.raises(AssertionError):
        _assert_force_fetch_seen([["git", "fetch", "origin", branch]], branch)


def test_no_fetch_at_all_black_sample_is_rejected():
    """完全沒有 fetch 命令時必須被拒絕——判別力黑樣本。"""
    branch = "deploy/test"

    with pytest.raises(AssertionError):
        _assert_force_fetch_seen([], branch)


def test_wrong_branch_force_refspec_black_sample_is_rejected():
    """force refspec 指向錯誤分支時必須被拒絕——判別力黑樣本。"""
    branch = "deploy/test"
    other = "deploy/other"

    with pytest.raises(AssertionError):
        _assert_force_fetch_seen(
            [["git", "fetch", "origin", f"+refs/heads/{other}:refs/remotes/origin/{other}"]],
            branch,
        )


def test_force_flag_not_refspec_black_sample_is_rejected():
    """`git fetch --force origin <branch>` 這種 `--force` 旗標形式必須被拒絕——判別力黑樣本。

    `--force` 旗標雖與 `+` refspec 前綴語意相近（都強制更新），但 argv 結構
    `["git", "fetch", "--force", "origin", branch]` 缺完整 refspec，無法讓 FETCH_HEAD
    精確定位 `refs/remotes/origin/<branch>`。helper 的 `expected in calls` 是整條 argv
    list 精確比對，`--force` 形式的 argv 與 `_force_fetch()` 產生的顯式 refspec 形式
    不相等，因此理所當然被拒，`match=` 鎖住 helper line 16 的 "missing force fetch argv" 前綴。

    紅樣本 mutation 證據：若把 `_force_fetch()` 改回回傳
    `["git", "fetch", "--force", "origin", branch]`，則本測試傳入的 argv 會等於 expected、
    `expected in calls` 成立、AssertionError 不再拋出，本測試翻紅——已於 task #2 實跑確認。
    """
    branch = "deploy/test"

    with pytest.raises(AssertionError, match=r"missing force fetch argv"):
        _assert_force_fetch_seen([["git", "fetch", "--force", "origin", branch]], branch)


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
