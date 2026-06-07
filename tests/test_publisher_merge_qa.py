"""QA 加固測試：針對任務 #1 驗收標準逐項驗證 publisher merge 行為。

涵蓋：
- 驗收 1：merge=True 才在 push 成功後呼叫 merge；merge=False/未設定行為不變。
- 驗收 2：merge 衝突/失敗不丟例外，merged=False 且帶可讀錯誤、token 已遮蔽。
- 額外：_merge_pr 各 HTTP 狀態碼（200/405/409/其他/網路例外）皆不丟例外。
- 額外：PublishResult.to_dict() 含 merged 欄位。
- 驗收 3：/api/health 可正常回應。
"""

from __future__ import annotations

import httpx
import pytest

from studio import config, publisher, runner


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "supersecrettoken")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")

    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(runner, "git_init", _noop)
    monkeypatch.setattr(runner, "git_commit", _noop)


@pytest.fixture
def _ok_push_pr(monkeypatch):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/7"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)


# --- 驗收 1：開關語意 -------------------------------------------------


@pytest.mark.asyncio
async def test_merge_only_after_push_success(monkeypatch, _configured):
    """push 失敗時不應嘗試 merge（即使 merge=True）。"""
    called = {"merge": 0}

    async def fail_push(cwd, branch, url):
        return runner.RunOutput("git push", 1, "denied supersecrettoken", False)

    async def spy_merge(number, payload):
        called["merge"] += 1
        return True, "sha"

    monkeypatch.setattr(publisher, "_push", fail_push)
    monkeypatch.setattr(publisher, "_merge_pr", spy_merge)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    assert not res.ok and not res.merged
    assert called["merge"] == 0
    assert "supersecrettoken" not in res.detail  # token 遮蔽


@pytest.mark.asyncio
async def test_merge_success_sets_flag_and_detail(monkeypatch, _configured, _ok_push_pr):
    async def fake_merge(number, payload):
        assert number == 7
        return True, "deadbeef"

    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    assert res.ok and res.merged and res.pr_number == 7
    assert res.to_dict()["merged"] is True


@pytest.mark.asyncio
async def test_merge_off_default_behaviour_unchanged(monkeypatch, _configured, _ok_push_pr):
    async def boom(number, payload):
        raise AssertionError("merge=False 不該呼叫 _merge_pr")

    monkeypatch.setattr(publisher, "_merge_pr", boom)
    res = await publisher.publish("/tmp", "s1", "需求")  # 預設 merge=False
    assert res.ok and res.pushed and not res.merged
    assert res.to_dict()["merged"] is False


# --- 驗收 2：衝突/失敗不丟例外 + token 遮蔽 ---------------------------


@pytest.mark.asyncio
async def test_merge_conflict_no_raise_redacted(monkeypatch, _configured, _ok_push_pr):
    async def conflict(number, payload):
        return False, "merge 衝突／不可合併（405）：not mergeable, token=supersecrettoken"

    monkeypatch.setattr(publisher, "_merge_pr", conflict)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    assert res.ok and res.pushed and not res.merged
    assert "supersecrettoken" not in res.detail
    assert "***" in res.detail


# --- _merge_pr 各狀態碼皆不丟例外 ------------------------------------


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _patch_put(monkeypatch, resp=None, exc=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put(self, url, json=None, headers=None):
            if exc:
                raise exc
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_merge_pr_200_ok(monkeypatch, _configured):
    _patch_put(monkeypatch, resp=_FakeResp(200, {"sha": "abc123"}))
    ok, info = await publisher._merge_pr(7, {"merge_method": "merge"})
    assert ok and info == "abc123"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [405, 409])
async def test_merge_pr_conflict_codes(monkeypatch, _configured, code):
    _patch_put(monkeypatch, resp=_FakeResp(code, text="not mergeable"))
    ok, info = await publisher._merge_pr(7, {})
    assert not ok and "衝突" in info


@pytest.mark.asyncio
async def test_merge_pr_other_failure(monkeypatch, _configured):
    _patch_put(monkeypatch, resp=_FakeResp(500, text="server error"))
    ok, info = await publisher._merge_pr(7, {})
    assert not ok and "merge 失敗" in info


@pytest.mark.asyncio
async def test_merge_pr_network_exception_no_raise(monkeypatch, _configured):
    _patch_put(monkeypatch, exc=httpx.ConnectError("boom"))
    ok, info = await publisher._merge_pr(7, {})
    assert not ok and "merge 請求失敗" in info


# --- 驗收 3：health endpoint -----------------------------------------


def test_health_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from studio.server import app

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    res = TestClient(app).get("/api/health")
    assert res.status_code == 200 and res.json()["ok"] is True
