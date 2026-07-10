from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from studio import config, git_cred


@pytest.fixture(autouse=True)
def _git_cred_legacy_off(monkeypatch):
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", False)


def test_make_env_with_no_url_is_still_pinned_to_github(monkeypatch):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)
    token = "ghp_no_url_secret"

    env = git_cred.make_env(token, url=None)

    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    assert token not in "".join(env.values())
    header_b64 = env["GIT_CONFIG_VALUE_1"].rsplit(" ", 1)[-1]
    assert base64.b64decode(header_b64).decode() == f"x-access-token:{token}"


def test_make_env_rejects_http_github_and_github_subdomains(monkeypatch):
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)

    assert git_cred.make_env("tok", url="http://github.com/owner/repo.git") == {}
    assert git_cred.make_env("tok", url="https://api.github.com/owner/repo.git") == {}
    assert git_cred.make_env("tok", url="https://github.com.evil.test/owner/repo.git") == {}


def test_git_env_support_detection_is_lazy_and_cached(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 5
        return SimpleNamespace(stdout="git version 2.31.0\n", stderr="")

    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", None)
    monkeypatch.setattr(git_cred.subprocess, "run", fake_run)

    assert git_cred.make_env("first")
    assert git_cred.make_env("second")
    assert calls == [["git", "--version"]]


def test_git_env_support_rejects_pre_231_without_leaking_plain_token(monkeypatch):
    def fake_run(argv, **kwargs):
        return SimpleNamespace(stdout="git version 2.30.9\n", stderr="")

    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", None)
    monkeypatch.setattr(git_cred.subprocess, "run", fake_run)

    assert git_cred.make_env("oldgit-token") == {}


def test_legacy_flag_false_spellings_reload_as_off(monkeypatch):
    for value in ("0", "false", "False", ""):
        monkeypatch.setenv("TI_GIT_CRED_LEGACY", value)
        config.reload()
        assert config.TI_GIT_CRED_LEGACY is False

    monkeypatch.delenv("TI_GIT_CRED_LEGACY", raising=False)
    config.reload()
