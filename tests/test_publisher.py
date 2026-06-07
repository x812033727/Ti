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
        return True, "https://github.com/o/r/pull/9", 9

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)

    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed
    assert res.branch == "ti-studio/s1"
    assert res.pr_url.endswith("/pull/9")
    assert res.merged is False  # 預設不 merge，維持現況行為


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
        return False, "PR 建立失敗（422）：unrelated histories", None

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed
    assert res.pr_url is None
    assert "PR 建立失敗" in res.detail


# --- 自動 merge（純邏輯 + 開關 + IO mock）-----------------------------


def test_merge_payload():
    p = publisher.merge_payload("做一個 BMI CLI", "squash")
    assert p["merge_method"] == "squash"
    assert "BMI" in p["commit_title"]
    # 非法 method 退回 merge
    assert publisher.merge_payload("x", "evil")["merge_method"] == "merge"


@pytest.mark.asyncio
async def test_publish_merge_success(monkeypatch, _configured):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9", 9

    async def fake_merge(number, payload):
        assert number == 9
        return True, "已自動合併", "abc1234"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)

    res = await publisher.publish("/tmp", "s1", "需求", do_merge=True)
    assert res.ok and res.pushed
    assert res.merged is True
    assert res.merge_sha == "abc1234"
    assert res.pr_url.endswith("/pull/9")


@pytest.mark.asyncio
async def test_publish_merge_off_keeps_current_behavior(monkeypatch, _configured):
    """do_merge=False（預設）時不得呼叫 _merge_pr，行為與現況一致。"""

    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9", 9

    called = {"merge": False}

    async def fake_merge(number, payload):
        called["merge"] = True
        return True, "", "x"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)

    res = await publisher.publish("/tmp", "s1", "需求")  # do_merge 預設 False
    assert res.ok and res.merged is False
    assert called["merge"] is False


@pytest.mark.asyncio
async def test_publish_merge_fail_ok_stays_true(monkeypatch, _configured):
    """merge 失敗：ok 維持 True（push 成功語意），merged=False，訊息遮蔽 token。"""

    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9", 9

    async def fake_merge(number, payload):
        return False, "merge 失敗（409）：branch protection 擋下，token tok 略過", None

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)

    res = await publisher.publish("/tmp", "s1", "需求", do_merge=True)
    assert res.ok and res.pushed  # ok 不因 merge 失敗翻盤
    assert res.merged is False
    assert "merge 失敗" in res.detail


@pytest.mark.asyncio
async def test_merge_pr_redacts_token_on_error(monkeypatch):
    """_merge_pr 對 API 錯誤訊息套用 redact，token 不外洩。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "secrettoken")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")

    class FakeResp:
        status_code = 409
        text = "conflict for secrettoken"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put(self, url, json, headers):
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    ok, msg, sha = await publisher._merge_pr(9, {"merge_method": "merge"})
    assert ok is False and sha is None
    assert "secrettoken" not in msg
    assert "***" in msg
