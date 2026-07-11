"""QA 驗收：任務 #4 publish 認證不洩漏，且真實 push 路徑無 TypeError。

此檔用本地 bare repo 實跑 git push，不碰網路；重點驗證 token/header 只存在
GIT_CONFIG_* env，不進 argv、RunOutput.command，也不持久化到 .git/config。
"""

from __future__ import annotations

import base64

import pytest

from studio import config, git_cred, publisher, runner


async def _run_ok(cwd, argv, **kwargs):
    out = await runner.run_command_exec(cwd, argv, sandbox=False, timeout=20, **kwargs)
    assert out.ok, out.output
    return out


def _assert_no_push_auth_argv(argv: list[str], token: str) -> None:
    header_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    rendered = "\0".join(argv)
    assert "-c" not in argv
    assert token not in rendered
    assert header_b64 not in rendered
    assert "Authorization:" not in rendered
    assert "x-access-token" not in rendered


@pytest.mark.asyncio
async def test_real_push_uses_env_auth_without_argv_or_git_config_leak(tmp_path, monkeypatch):
    token = "secrettoken"
    header_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", False)
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)
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


@pytest.mark.asyncio
async def test_legacy_git_push_and_repush_keep_auth_out_of_argv(monkeypatch, tmp_path):
    token = "legacytoken"
    header_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    branch = "ti-studio/legacy-auth"
    monkeypatch.setattr(config, "GITHUB_TOKEN", token)
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", False)
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", False)

    assert publisher.git_auth_env(token) == {}

    black_sample = ["git", *git_cred.git_cred_argv(token), "push", "-u", "ti_publish", branch]
    with pytest.raises(AssertionError):
        _assert_no_push_auth_argv(black_sample, token)

    calls = []

    async def spy_exec(cwd, argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return runner.RunOutput(
            command=kwargs.get("label", argv[0]),
            exit_code=0,
            output="ok",
            timed_out=False,
        )

    monkeypatch.setattr(runner, "run_command_exec", spy_exec)

    pushed = await publisher._push(
        tmp_path,
        branch,
        "https://github.com/octo/repo.git",
        env=publisher.git_auth_env(token),
    )
    repushed = await publisher.repush(tmp_path, branch)

    assert pushed.ok
    assert repushed.ok
    assert pushed.command == "git push"
    assert repushed.command == "git push"
    assert token not in pushed.command
    assert token not in repushed.command
    assert header_b64 not in pushed.command
    assert header_b64 not in repushed.command

    push_calls = [call for call in calls if call[1]["label"] == "git push"]
    assert push_calls == [
        (
            ["git", "push", "-u", "ti_publish", branch],
            {
                "timeout": 120,
                "sandbox": False,
                "label": "git push",
                "env": {},
            },
        ),
        (
            ["git", "push", "ti_publish", branch],
            {
                "timeout": 120,
                "sandbox": False,
                "label": "git push",
                "env": {},
            },
        ),
    ]
    for argv, kwargs in push_calls:
        _assert_no_push_auth_argv(argv, token)
        assert token not in kwargs["label"]
        assert header_b64 not in kwargs["label"]
