"""測試成果發佈 publisher（純邏輯 + 以 mock 取代實際 IO）。"""

from __future__ import annotations

import pytest

from studio import config, publisher, runner

# --- 純邏輯 -------------------------------------------------------------


def test_is_configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    assert not publisher.is_configured()
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")
    assert publisher.is_configured()


def test_branch_name():
    assert publisher.branch_name("abc123") == "ti-studio/abc123"
    assert publisher.branch_name("../evil id") == "ti-studio/evilid"


def test_remote_url_and_redact():
    url = publisher.remote_url("octo/repo", "secrettoken")
    assert url == "https://x-access-token:secrettoken@github.com/octo/repo.git"
    assert "secrettoken" not in publisher.redact(url, "secrettoken")
    assert "***" in publisher.redact(url, "secrettoken")


def test_pr_payload():
    p = publisher.pr_payload("做一個 BMI CLI", "ti-studio/x", "main")
    assert p["head"] == "ti-studio/x"
    assert p["base"] == "main"
    assert "BMI" in p["title"]


def test_parse_pr_number():
    assert publisher.parse_pr_number("https://github.com/o/r/pull/42") == 42
    assert publisher.parse_pr_number("https://github.com/o/r/pull/9?foo=1") == 9
    assert publisher.parse_pr_number("") is None
    assert publisher.parse_pr_number(None) is None
    assert publisher.parse_pr_number("https://example.com/no-number") is None


def test_merge_payload():
    p = publisher.merge_payload("ti-studio/x")
    assert p["merge_method"] == "merge"
    assert "ti-studio/x" in p["commit_title"]


# --- publish 流程（mock IO）-------------------------------------------


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")

    # 跳過實際 git init/commit
    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(runner, "git_init", _noop)
    monkeypatch.setattr(runner, "git_commit", _noop)


@pytest.mark.asyncio
async def test_publish_not_configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    res = await publisher.publish("/tmp", "s1", "需求")
    assert not res.ok
    assert "未設定" in res.detail


@pytest.mark.asyncio
async def test_publish_push_then_pr(monkeypatch, _configured):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)

    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed
    assert res.branch == "ti-studio/s1"
    assert res.pr_url.endswith("/pull/9")


@pytest.mark.asyncio
async def test_publish_push_fail(monkeypatch, _configured):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(
            command="git push",
            exit_code=1,
            output="remote: token tok denied",
            timed_out=False,
        )

    monkeypatch.setattr(publisher, "_push", fake_push)
    res = await publisher.publish("/tmp", "s1", "需求")
    assert not res.ok
    assert "push 失敗" in res.detail
    assert "tok" not in res.detail  # token 已遮蔽


@pytest.mark.asyncio
async def test_publish_pr_fail_still_ok(monkeypatch, _configured):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return False, "PR 建立失敗（422）：unrelated histories"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed
    assert res.pr_url is None
    assert "PR 建立失敗" in res.detail


# --- merge 流程（mock IO）--------------------------------------------


@pytest.fixture
def _ok_push_pr(monkeypatch):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/7"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)


@pytest.mark.asyncio
async def test_publish_merge_off_does_not_merge(monkeypatch, _configured, _ok_push_pr):
    called = {"n": 0}

    async def fake_merge(number, payload):
        called["n"] += 1
        return True, "deadbeef"

    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    # 預設 merge=False → 不應呼叫 _merge_pr，行為與現在相同
    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed and not res.merged
    assert res.pr_url.endswith("/pull/7")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_publish_merge_success(monkeypatch, _configured, _ok_push_pr):
    async def fake_merge(number, payload):
        assert number == 7
        return True, "deadbeef"

    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    assert res.ok and res.merged
    assert res.pr_number == 7
    assert "合併" in res.detail


@pytest.mark.asyncio
async def test_publish_merge_conflict_no_raise(monkeypatch, _configured, _ok_push_pr):
    async def fake_merge(number, payload):
        return False, "merge 衝突／不可合併（405）：not mergeable"

    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    # 衝突不丟例外：仍 ok（已 push/開 PR），但 merged=False 且帶可讀錯誤
    assert res.ok and res.pushed and not res.merged
    assert res.pr_url.endswith("/pull/7")
    assert "衝突" in res.detail
    assert "tok" not in res.detail  # token 已遮蔽
