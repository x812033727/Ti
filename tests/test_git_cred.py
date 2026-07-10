from __future__ import annotations

import base64

import pytest

from studio import config, git_cred


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
