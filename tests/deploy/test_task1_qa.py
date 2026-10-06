"""QA 驗證：任務 #1 實作需求的回歸風險。

重點不是再測快樂路徑，而是卡住兩個容易回頭壞掉的邊界：
- _prepare_clone() 不可退回裸 `git fetch origin <branch>`。
- redeploy() 成功重啟路徑必須經過可 mock 的 import_smoke()，測試不可誤跑真子程序。
"""

from __future__ import annotations

import pytest

from studio import autopilot, config, redeploy, runner


@pytest.mark.asyncio
async def test_prepare_clone_fetch_has_force_refspec_and_no_bare_branch_token(
    tmp_path, monkeypatch
):
    branch = "qa/branch-with-slash"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[tuple[list[str], dict]] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot, "_git_cred_argv", lambda: ["-c", "http.extraheader=AUTH"])
    monkeypatch.setattr(autopilot, "_git_cred_env", lambda: {"QA_GIT_ENV": "1"})

    async def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return 0, ""

    monkeypatch.setattr(autopilot, "_run", fake_run)

    assert await autopilot._prepare_clone(str(work)) == str(work)

    fetch_calls = [(cmd, kwargs) for cmd, kwargs in calls if "fetch" in cmd]
    assert len(fetch_calls) == 1
    fetch_cmd, fetch_kwargs = fetch_calls[0]
    assert fetch_cmd == [
        "git",
        "-c",
        "http.extraheader=AUTH",
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    ]
    assert branch not in fetch_cmd
    assert fetch_kwargs["cwd"] == str(work)
    assert fetch_kwargs["timeout"] == 120
    assert fetch_kwargs["env"] == {"QA_GIT_ENV": "1"}


@pytest.mark.asyncio
async def test_redeploy_success_path_uses_mockable_import_smoke(monkeypatch, tmp_path):
    calls = {"smoke": 0, "restart": 0}

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)

    async def fail_if_real_subprocess(*_args, **_kwargs):
        raise AssertionError("redeploy success path must not run a real subprocess in tests")

    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    async def fake_import_smoke():
        calls["smoke"] += 1
        return runner.RunOutput("import smoke", 0, "", False)

    monkeypatch.setattr(runner, "run_command_exec", fail_if_real_subprocess)
    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    monkeypatch.setattr(redeploy, "import_smoke", fake_import_smoke)
    monkeypatch.setattr(
        redeploy,
        "schedule_restart",
        lambda *args, **kwargs: calls.__setitem__("restart", calls["restart"] + 1),
    )

    result = await redeploy.redeploy()

    assert result["ok"] is True
    assert result["pulled"] is True
    assert result["restarting"] is True
    assert calls == {"smoke": 1, "restart": 1}
