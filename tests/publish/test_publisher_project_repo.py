"""per-project 發佈 repo：contextvar 覆寫、自動建 repo／空 repo 初始化、PR 走覆寫目標。"""

from __future__ import annotations

import pytest

from studio import config, publisher, runner


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "global/repo")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")
    # owner allowlist 護欄：放行本檔測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"global", "me", "other"}))


def _ok_run(label="git"):
    return runner.RunOutput(command=label, exit_code=0, output="ok", timed_out=False)


@pytest.fixture
def _git_ok(monkeypatch):
    async def fake_init(cwd):
        return _ok_run("git init")

    async def fake_commit(cwd, msg):
        return _ok_run("git commit")

    monkeypatch.setattr(runner, "git_init", fake_init)
    monkeypatch.setattr(runner, "git_commit", fake_commit)


# --- current_repo / 覆寫語義 ---------------------------------------------


def test_current_repo_falls_back_to_global(_configured):
    assert publisher.current_repo() == "global/repo"
    token = publisher.set_repo_override("me/product")
    try:
        assert publisher.current_repo() == "me/product"
        assert publisher.is_configured()
    finally:
        publisher.reset_repo_override(token)
    assert publisher.current_repo() == "global/repo"


def test_override_makes_publish_configured_without_global(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")  # 全域未設
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"me"}))
    assert not publisher.is_configured()
    token = publisher.set_repo_override("me/product")
    try:
        assert publisher.is_configured()  # 專案自己的 repo 即可發佈
    finally:
        publisher.reset_repo_override(token)


# --- publish(repo=...) 三條路 --------------------------------------------


@pytest.mark.asyncio
async def test_publish_project_repo_ready_opens_pr_there(monkeypatch, _configured, _git_ok):
    seen = {}

    async def fake_ensure(repo, base):
        seen["ensure"] = (repo, base)
        return "ready"

    async def fake_push(cwd, branch, url):
        seen["push_url"] = url
        return _ok_run("git push")

    async def fake_pr(payload):
        seen["api_repo"] = publisher.current_repo()  # PR 階段 contextvar 應仍是覆寫值
        return True, "https://github.com/me/product/pull/3"

    monkeypatch.setattr(publisher, "_ensure_repo", fake_ensure)
    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)

    res = await publisher.publish("/tmp", "s1", "需求", repo="me/product")

    assert res.ok and res.pushed and res.repo == "me/product"
    assert seen["ensure"] == ("me/product", "main")
    assert "me/product" in seen["push_url"]
    assert seen["api_repo"] == "me/product"
    assert publisher.current_repo() == "global/repo"  # publish 結束後覆寫已還原


@pytest.mark.asyncio
async def test_publish_project_repo_empty_initializes_base(monkeypatch, _configured, _git_ok):
    async def fake_ensure(repo, base):
        return "empty"

    pushed = {}

    async def fake_push_base(cwd, base, url):
        pushed["base"] = base
        return _ok_run("git push init")

    monkeypatch.setattr(publisher, "_ensure_repo", fake_ensure)
    monkeypatch.setattr(publisher, "_push_base", fake_push_base)

    res = await publisher.publish("/tmp", "s1", "需求", repo="me/newrepo")

    assert res.ok and res.pushed and res.merged  # 成果已在主分支
    assert pushed["base"] == "main"
    assert "首次發佈" in res.detail and res.pr_url is None


@pytest.mark.asyncio
async def test_publish_project_repo_unavailable_fails_clearly(monkeypatch, _configured, _git_ok):
    async def fake_ensure(repo, base):
        return "unavailable: repo 不存在，且 owner 非 token 使用者，無法自動建立"

    monkeypatch.setattr(publisher, "_ensure_repo", fake_ensure)
    res = await publisher.publish("/tmp", "s1", "需求", repo="other/repo")
    assert not res.ok
    assert "無法發佈" in res.detail and "owner" in res.detail


@pytest.mark.asyncio
async def test_publish_without_override_skips_ensure(monkeypatch, _configured, _git_ok):
    """全域 repo 路徑維持原行為：不做存在性檢查。"""
    called = {"ensure": 0}

    async def fake_ensure(repo, base):
        called["ensure"] += 1
        return "ready"

    async def fake_push(cwd, branch, url):
        return _ok_run("git push")

    monkeypatch.setattr(publisher, "_ensure_repo", fake_ensure)
    monkeypatch.setattr(publisher, "_push", fake_push)
    res = await publisher.publish("/tmp", "s1", "需求", make_pr=False)
    assert res.ok and called["ensure"] == 0
