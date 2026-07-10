from __future__ import annotations

import base64
import hashlib
import sys

import pytest

from studio import config, git_cred, runner


@pytest.fixture(autouse=True)
def _git_cred_legacy_off(monkeypatch):
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", False)


def test_make_env_clears_helpers_before_github_extraheader(monkeypatch):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)
    token = "secrettoken"

    env = git_cred.make_env(token)

    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    expected_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_1"] == f"Authorization: Basic {expected_b64}"
    assert token not in "".join(env.values())


def test_make_env_empty_or_non_github_url_returns_empty(monkeypatch):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)

    assert git_cred.make_env("") == {}
    assert git_cred.make_env(None) == {}
    assert git_cred.make_env("tok", url="https://example.com/owner/repo.git") == {}


def test_make_env_scopes_to_github_and_overwrites_parent_indexes(monkeypatch):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)

    env = git_cred.make_env(
        "tok",
        url="https://x-access-token:old@github.com/owner/repo.git",
    )

    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"


@pytest.mark.asyncio
async def test_make_env_overrides_parent_git_config_when_merged(monkeypatch, tmp_path):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.https://evil.example/.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Basic old")
    token = "secrettoken"

    env = git_cred.make_env(token, url="https://github.com/owner/repo.git")
    probe_env = {
        **env,
        "EXPECTED_HEADER_SHA": hashlib.sha256(env["GIT_CONFIG_VALUE_1"].encode()).hexdigest(),
    }
    probe = (
        "import hashlib, os, sys\n"
        "header = os.environ.get('GIT_CONFIG_VALUE_1', '')\n"
        "ok = (\n"
        "    os.environ.get('GIT_CONFIG_COUNT') == '2'\n"
        "    and os.environ.get('GIT_CONFIG_KEY_0') == 'credential.helper'\n"
        "    and os.environ.get('GIT_CONFIG_VALUE_0') == ''\n"
        "    and os.environ.get('GIT_CONFIG_KEY_1') == 'http.https://github.com/.extraheader'\n"
        "    and hashlib.sha256(header.encode()).hexdigest()\n"
        "    == os.environ.get('EXPECTED_HEADER_SHA')\n"
        ")\n"
        "print('env-overridden' if ok else 'env-stale')\n"
        "sys.exit(0 if ok else 1)\n"
    )

    out = await runner.run_command_exec(
        tmp_path,
        [sys.executable, "-c", probe],
        timeout=10,
        sandbox=False,
        label="git cred env probe",
        env=probe_env,
    )

    assert out.ok
    assert out.output.strip() == "env-overridden"
    assert out.command == "git cred env probe"
    assert token not in out.command
    assert token not in probe
    assert base64.b64encode(f"x-access-token:{token}".encode()).decode() not in probe


def test_make_env_returns_empty_when_git_config_env_unsupported(monkeypatch):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", False)

    assert git_cred.make_env("tok") == {}


def test_git_cred_argv_is_only_for_unsupported_git(monkeypatch):
    token = "secrettoken"
    expected_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()

    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)
    assert git_cred.git_cred_argv(token) == []

    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", False)
    assert git_cred.git_cred_argv(token) == [
        "-c",
        "credential.helper=",
        "-c",
        f"http.https://github.com/.extraheader=Authorization: Basic {expected_b64}",
    ]
    assert git_cred.git_cred_argv(token, url="https://example.com/owner/repo.git") == []


def test_git_cred_legacy_flag_disables_env_and_forces_argv_without_git_probe(
    monkeypatch,
):
    token = "legacy-secret"
    expected_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()

    def fail_git_probe(*args, **kwargs):
        raise AssertionError("legacy mode must not probe git version")

    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", True)
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", None)
    monkeypatch.setattr(git_cred.subprocess, "run", fail_git_probe)

    assert git_cred.make_env(token) == {}
    assert git_cred.git_cred_argv(token) == [
        "-c",
        "credential.helper=",
        "-c",
        f"http.https://github.com/.extraheader=Authorization: Basic {expected_b64}",
    ]
    assert "base64" in (git_cred.git_cred_argv.__doc__ or "")
    assert "ps" in (git_cred.git_cred_argv.__doc__ or "")


def test_clean_url_removes_userinfo_without_touching_path_query_or_fragment():
    url = "https://x-access-token:tok@github.com:443/owner/repo.git?x=1#frag"

    assert git_cred.clean_url(url) == "https://github.com:443/owner/repo.git?x=1#frag"
    assert git_cred.clean_url("https://github.com/owner/repo.git") == (
        "https://github.com/owner/repo.git"
    )
    assert git_cred.clean_url("git@github.com:owner/repo.git") == "git@github.com:owner/repo.git"


def test_git_cred_legacy_config_default_and_reload(monkeypatch):
    monkeypatch.delenv("TI_GIT_CRED_LEGACY", raising=False)
    config.reload()
    assert config.TI_GIT_CRED_LEGACY is False

    monkeypatch.setenv("TI_GIT_CRED_LEGACY", "1")
    config.reload()
    assert config.TI_GIT_CRED_LEGACY is True

    monkeypatch.delenv("TI_GIT_CRED_LEGACY", raising=False)
    config.reload()
