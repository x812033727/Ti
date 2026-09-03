"""QA for 任務 #1：`_prepare_clone` 的 force-fetch 合約與失敗路徑。

驗收重點：
1. fetch 必須使用 force refspec，避免 remote-tracking ref CAS 競爭。
2. fetch 失敗時不得繼續 reset 到可能過期的 `origin/<branch>`。
"""

from __future__ import annotations

import pytest

from studio import autopilot


@pytest.mark.asyncio
async def test_prepare_clone_fetch_uses_force_refspec_for_slash_branch(tmp_path, monkeypatch):
    branch = "release/2026.09"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_git_cred_argv", lambda: [])
    monkeypatch.setattr(autopilot, "_git_cred_env", lambda: None)

    async def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return 0, ""

    monkeypatch.setattr(autopilot, "_run", fake_run)

    assert await autopilot._prepare_clone(str(work)) == str(work)

    assert ["git", "fetch", "origin", branch] not in calls
    assert [
        "git",
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    ] in calls


@pytest.mark.asyncio
async def test_prepare_clone_fetch_failure_does_not_reset_stale_origin(tmp_path, monkeypatch):
    branch = "main"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_git_cred_argv", lambda: [])
    monkeypatch.setattr(autopilot, "_git_cred_env", lambda: None)

    async def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if list(cmd) == [
            "git",
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ]:
            return 128, "fatal: cannot lock ref 'refs/remotes/origin/main'"
        return 0, ""

    monkeypatch.setattr(autopilot, "_run", fake_run)

    with pytest.raises(RuntimeError, match="fetch|同步|取得"):
        await autopilot._prepare_clone(str(work))

    fetch_index = calls.index(
        [
            "git",
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ]
    )
    assert calls[fetch_index + 1 :] == [], (
        "fetch 失敗後不得 reset/clean/config，避免使用 stale origin"
    )
