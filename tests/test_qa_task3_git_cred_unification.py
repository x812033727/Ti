from __future__ import annotations

import base64

import pytest

from studio import autopilot, config, git_cred, publisher

TOKEN = "qa-task3-token"


@pytest.fixture(autouse=True)
def _new_git_cred_path(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", False)
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)


def _expected_b64() -> str:
    return base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


def _assert_new_auth_env(env: dict[str, str] | None) -> None:
    assert env is not None
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_1"] == f"Authorization: Basic {_expected_b64()}"


def _assert_no_cred_argv(cmd: list[str]) -> None:
    rendered = "\0".join(cmd)
    assert "gh auth git-credential" not in rendered
    assert "Authorization:" not in rendered
    assert "x-access-token" not in rendered
    assert TOKEN not in rendered
    assert _expected_b64() not in rendered


def test_publisher_git_auth_env_delegates_to_git_cred(monkeypatch):
    calls: list[tuple[str | None, str | None]] = []

    def fake_make_env(token: str | None, url: str | None = None) -> dict[str, str]:
        calls.append((token, url))
        return {"sentinel": "delegated"}

    monkeypatch.setattr(publisher.git_cred, "make_env", fake_make_env)

    assert publisher.git_auth_env("tok") == {"sentinel": "delegated"}
    assert calls == [("tok", None)]


def test_autopilot_uses_git_cred_env_not_gh_helper():
    assert autopilot._git_cred_argv() == []
    env = autopilot._git_cred_env()

    _assert_new_auth_env(env)
    assert "gh auth git-credential" not in " ".join(env.values())


def test_autopilot_fallback_argv_still_comes_from_git_cred_without_gh(monkeypatch):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", False)

    argv = autopilot._git_cred_argv()

    assert argv[:3] == ["-c", "credential.helper=", "-c"]
    assert (
        argv[-1] == f"http.https://github.com/.extraheader=Authorization: Basic {_expected_b64()}"
    )
    assert "gh auth git-credential" not in "\0".join(argv)


@pytest.mark.asyncio
async def test_prepare_clone_and_fetch_keep_argv_clean_and_auth_in_env(monkeypatch, tmp_path):
    calls: list[dict] = []
    work = tmp_path / "work"

    async def fake_run(cmd, cwd=None, timeout=600, env=None):
        calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout, "env": env})
        return (0, "")

    monkeypatch.setattr(config, "AUTOPILOT_WORK_DIR", str(work))
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "owner/repo")
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(autopilot, "_run", fake_run)

    assert await autopilot._prepare_clone() == str(work)

    clone = calls[0]
    fetch = next(call for call in calls if call["cmd"][1:3] == ["fetch", "origin"])
    assert clone["cmd"] == [
        "git",
        "clone",
        "https://github.com/owner/repo.git",
        str(work),
    ]
    assert fetch["cmd"] == ["git", "fetch", "origin", "main"]

    for call in (clone, fetch):
        _assert_no_cred_argv(call["cmd"])
        _assert_new_auth_env(call["env"])


@pytest.mark.asyncio
async def test_commit_push_merge_keeps_push_shape_and_moves_auth_to_env(monkeypatch):
    calls: list[dict] = []
    task = {"id": "3", "title": "task three", "detail": ""}
    branch = "autopilot/task-3"

    async def fake_run(cmd, cwd=None, timeout=600, env=None):
        calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout, "env": env})
        joined = " ".join(cmd)
        if "remote get-url --push origin" in joined:
            return (0, "https://github.com/owner/repo.git")
        if "rev-list --count" in joined:
            return (0, "1")
        if "pr view" in joined:
            return (0, "33")
        return (0, "")

    async def fake_merge_flow(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "sha")

    monkeypatch.setattr(config, "AUTOPILOT_REPO", "owner/repo")
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_RECLAIM_BRANCH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)
    monkeypatch.setattr(config, "AUTOPILOT_AUTO_MERGE", False)
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"owner"}))
    monkeypatch.setattr(autopilot, "_run", fake_run)
    monkeypatch.setattr(publisher, "_merge_flow", fake_merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", task)

    assert ok is True, msg
    lsremote = next(call for call in calls if "ls-remote" in call["cmd"])
    push = next(call for call in calls if "push" in call["cmd"])
    assert lsremote["cmd"] == ["git", "ls-remote", "--heads", "origin", branch]
    assert push["cmd"] == ["git", "push", "-u", "origin", branch]

    for call in (lsremote, push):
        _assert_no_cred_argv(call["cmd"])
        _assert_new_auth_env(call["env"])


def test_autopilot_empty_token_injects_no_auth(monkeypatch):
    """前置條件守門：GITHUB_TOKEN 為空時 autopilot git 操作不帶任何認證（argv 空、env 繼承 os.environ）。

    這鎖住「移除 gh helper 後認證綁 GITHUB_TOKEN」的行為：token 缺席就沒有認證（與無 gh 登入
    的舊行為等價），私有 repo 會失敗——而非靜默走到別的認證來源。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")

    assert autopilot._git_cred_argv() == []
    assert autopilot._git_cred_env() is None


def test_autopilot_legacy_valve_forces_argv_and_empties_env(monkeypatch):
    """legacy 閥開啟：env 注入關閉（回 None → _run 繼承 os.environ），改由 argv fallback 承接認證。"""
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", True)

    argv = autopilot._git_cred_argv()
    assert argv[:3] == ["-c", "credential.helper=", "-c"]
    assert argv[-1].startswith("http.https://github.com/.extraheader=Authorization: Basic ")
    assert autopilot._git_cred_env() is None


@pytest.mark.asyncio
async def test_publisher_push_new_mechanism_clean_argv_auth_in_env(monkeypatch):
    """新機制下 publisher push：argv 不帶憑證（git_cred_argv 回 []），認證走 env extraHeader。"""
    seen: list[dict] = []

    async def fake_exec(cwd, cmd, *, timeout, sandbox, label, env=None):
        seen.append({"cmd": list(cmd), "label": label, "env": env})
        return publisher.runner.RunOutput(command=label, exit_code=0, output="", timed_out=False)

    monkeypatch.setattr(publisher.runner, "run_command_exec", fake_exec)
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"owner"}))

    await publisher._push(
        "/cwd",
        "feat",
        publisher.remote_url("owner/repo"),
        env=publisher.git_auth_env(config.GITHUB_TOKEN),
    )

    push = next(c for c in seen if "push" in c["cmd"])
    assert push["cmd"] == ["git", "push", "-u", "ti_publish", "feat"]
    _assert_no_cred_argv(push["cmd"])
    _assert_new_auth_env(push["env"])


@pytest.mark.asyncio
async def test_publisher_push_fallback_argv_carries_auth_when_env_unsupported(monkeypatch):
    """legacy/舊 git（env 不可用）下 publisher push：argv fallback 帶 extraHeader 承接認證，push 不斷鏈。"""
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", False)
    seen: list[dict] = []

    async def fake_exec(cwd, cmd, *, timeout, sandbox, label, env=None):
        seen.append({"cmd": list(cmd), "label": label, "env": env})
        return publisher.runner.RunOutput(command=label, exit_code=0, output="", timed_out=False)

    monkeypatch.setattr(publisher.runner, "run_command_exec", fake_exec)
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"owner"}))

    await publisher._push(
        "/cwd",
        "feat",
        publisher.remote_url("owner/repo"),
        env=publisher.git_auth_env(config.GITHUB_TOKEN),
    )

    push = next(c for c in seen if "push" in c["cmd"])
    # env 不可用 → git_auth_env(=make_env) 回 {}，認證改由 argv 的 -c extraHeader 承接。
    assert push["env"] == {}
    assert push["cmd"][:4] == ["git", "-c", "credential.helper=", "-c"]
    assert push["cmd"][4].startswith("http.https://github.com/.extraheader=Authorization: Basic ")
    assert push["cmd"][-4:] == ["push", "-u", "ti_publish", "feat"]


def test_git_cred_exposes_public_auth_b64_for_redact():
    """redact 依賴的 base64 編碼是 git_cred 公開 API（非私有底線符號），封裝不破口。"""
    assert "auth_b64" in git_cred.__all__
    assert git_cred.auth_b64(TOKEN) == _expected_b64()
    # publisher.redact 委派公開 API，遮蔽 token 的 base64 形式。
    leak = f"noise {_expected_b64()} tail"
    assert git_cred.auth_b64(TOKEN) not in publisher.redact(leak, TOKEN)
