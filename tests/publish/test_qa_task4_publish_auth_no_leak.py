"""QA 驗收：任務 #4 publish 認證不洩漏，且真實 push 路徑無 TypeError。

此檔用本地 bare repo 實跑 git push，不碰網路；重點驗證 token/header 只存在
GIT_CONFIG_* env，不進 argv、RunOutput.command，也不持久化到 .git/config。
"""

from __future__ import annotations

import base64

import pytest

from studio import publisher, runner


async def _run_ok(cwd, argv, **kwargs):
    out = await runner.run_command_exec(cwd, argv, sandbox=False, timeout=20, **kwargs)
    assert out.ok, out.output
    return out


@pytest.mark.asyncio
async def test_real_push_uses_env_auth_without_argv_or_git_config_leak(tmp_path, monkeypatch):
    token = "secrettoken"
    header_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    auth_env = publisher.git_auth_env(token)
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()

    await _run_ok(tmp_path, ["git", "init", "--bare", str(remote)])
    await _run_ok(work, ["git", "init", "-q"])
    await _run_ok(work, ["git", "config", "user.email", "qa@example.invalid"])
    await _run_ok(work, ["git", "config", "user.name", "QA"])
    (work / "README.md").write_text("qa\n", encoding="utf-8")
    await _run_ok(work, ["git", "add", "README.md"])
    await _run_ok(work, ["git", "commit", "-m", "init"])

    real_exec = runner.run_command_exec
    calls = []

    async def spy_exec(cwd, argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return await real_exec(cwd, argv, **kwargs)

    monkeypatch.setattr(runner, "run_command_exec", spy_exec)

    pushed = await publisher._push(work, "ti-studio/qa-auth", str(remote), env=auth_env)

    assert pushed.ok, pushed.output
    assert calls[-1][0] == ["git", "push", "-u", "ti_publish", "ti-studio/qa-auth"]
    assert calls[-1][1]["env"] == auth_env

    for argv, kwargs in calls:
        rendered_argv = "\0".join(argv)
        assert token not in rendered_argv
        assert header_b64 not in rendered_argv
        assert "Authorization:" not in rendered_argv
        assert token not in kwargs["label"]
        assert header_b64 not in kwargs["label"]

    remote_url = await real_exec(
        work,
        ["git", "remote", "get-url", "ti_publish"],
        sandbox=False,
        timeout=20,
        label="git remote get-url",
    )
    local_config = await real_exec(
        work,
        ["git", "config", "--local", "--list"],
        sandbox=False,
        timeout=20,
        label="git config local list",
    )

    assert remote_url.ok, remote_url.output
    assert remote_url.output.strip() == str(remote)
    assert token not in remote_url.output
    assert header_b64 not in remote_url.output
    assert "x-access-token" not in remote_url.output

    assert local_config.ok, local_config.output
    assert token not in local_config.output
    assert header_b64 not in local_config.output
    assert "x-access-token" not in local_config.output
    assert "http.https://github.com/.extraheader" not in local_config.output
