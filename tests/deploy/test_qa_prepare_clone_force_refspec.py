"""QA：`_prepare_clone` 的 fetch 必須固定走 force refspec。

這裡專測工程師這輪補的 autopilot working clone 入口，不打真實 git/network。
"""

from __future__ import annotations

import pytest

from studio import autopilot


@pytest.mark.asyncio
async def test_prepare_clone_legacy_credential_argv_keeps_force_refspec(tmp_path, monkeypatch):
    """邊界：legacy git credential 會插入 `-c`，fetch 仍不得退回裸 branch。"""
    branch = "release/hotfix"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    async def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return 0, ""

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_git_cred_argv", lambda: ["-c", "credential.helper="])
    monkeypatch.setattr(autopilot, "_run", fake_run)

    assert await autopilot._prepare_clone(str(work)) == str(work)

    fetches = [cmd for cmd in calls if "fetch" in cmd]
    assert fetches == [
        [
            "git",
            "-c",
            "credential.helper=",
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ]
    ]
    assert ["git", "-c", "credential.helper=", "fetch", "origin", branch] not in calls


@pytest.mark.asyncio
async def test_prepare_clone_pin_failure_reports_force_fetch_failure(tmp_path, monkeypatch):
    """錯誤路徑：admission pin 找不到物件時，回報 fetch 失敗且 fetch argv 是 force refspec。"""
    branch = "main"
    expected_sha = "a" * 40
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    async def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "fetch" in cmd:
            return 128, "fatal: cannot lock ref 'refs/remotes/origin/main'"
        if cmd[:3] == ["git", "cat-file", "-e"]:
            return 128, "missing object"
        return 0, ""

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        await autopilot._prepare_clone(str(work), repo_sha=expected_sha)

    assert "admission SHA 不存在於工作 clone" in str(excinfo.value)
    assert "cannot lock ref" in str(excinfo.value)
    assert [
        "git",
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    ] in calls
    assert ["git", "fetch", "origin", branch] not in calls
