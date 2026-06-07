"""repo clone 純邏輯測試（不碰網路）：網址驗證與 token 注入。"""

from __future__ import annotations

from studio import runner


def test_is_valid_repo_url():
    assert runner.is_valid_repo_url("https://github.com/owner/repo")
    assert runner.is_valid_repo_url("https://github.com/owner/repo.git")
    assert runner.is_valid_repo_url("https://github.com/owner/repo/")
    # 僅接受 github.com 的 https
    assert not runner.is_valid_repo_url("http://github.com/owner/repo")
    assert not runner.is_valid_repo_url("https://evil.com/owner/repo")
    assert not runner.is_valid_repo_url("git@github.com:owner/repo.git")
    assert not runner.is_valid_repo_url("")


def test_build_clone_url_injects_token():
    url = "https://github.com/owner/repo.git"
    assert (
        runner.build_clone_url(url, "tok") == "https://x-access-token:tok@github.com/owner/repo.git"
    )
    assert runner.build_clone_url(url, None) == url
    assert runner.build_clone_url(url, "") == url
