"""QA：聚焦任務 #1（publisher 自動 merge 能力）的補強驗證。

涵蓋：
- merge 成功回傳含 merged/merge_sha/pr_url（commit/PR 連結）。
- 沿用 GITHUB_TOKEN：_merge_pr / _open_pr 的 Authorization header 帶同一把 token。
- merge 失敗各情境（403 權限、422、500 API 錯誤、網路例外、405 重試耗盡）回傳明確錯誤，服務不崩潰。
- 任何輸出（含 to_dict 全欄位）不得出現 token，一律經 redact。
"""

from __future__ import annotations

import httpx
import pytest

from studio import config, publisher, runner


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "secrettoken")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")

    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(runner, "git_init", _noop)
    monkeypatch.setattr(runner, "git_commit", _noop)


# --- 可注入的假 httpx Client，記錄送出的 headers ---------------------------


class _Resp:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    """每次建立都記到 captured；put/post 回傳預先排好的回應序列。"""

    def __init__(self, responses, captured):
        self._responses = list(responses)
        self._captured = captured

    def __call__(self, *a, **k):  # httpx.AsyncClient(timeout=...) 呼叫點
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def put(self, url, json, headers):
        self._captured.append({"method": "PUT", "url": url, "headers": headers, "json": json})
        return self._responses.pop(0)

    async def post(self, url, json, headers):
        self._captured.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


# --- merge 成功：回傳值完整 ----------------------------------------------


@pytest.mark.asyncio
async def test_merge_success_returns_sha_and_reuses_token(monkeypatch, _configured):
    captured = []
    monkeypatch.setattr(
        httpx, "AsyncClient", _FakeClient([_Resp(200, payload={"sha": "deadbeef"})], captured)
    )
    ok, msg, sha = await publisher._merge_pr(42, {"merge_method": "merge"})
    assert ok is True
    assert sha == "deadbeef"
    # 沿用 GITHUB_TOKEN：Authorization 帶同一把 token
    assert captured[0]["headers"]["Authorization"] == "Bearer secrettoken"
    # 打到正確的 merge endpoint
    assert captured[0]["url"].endswith("/repos/o/r/pulls/42/merge")


# --- merge 失敗各情境：明確錯誤 + 遮蔽 token + 不崩潰 --------------------


@pytest.mark.parametrize(
    "status",
    [403, 409, 422, 500],
)
@pytest.mark.asyncio
async def test_merge_fail_statuses_redacted(monkeypatch, _configured, status):
    captured = []
    body = f"error involving secrettoken at {status}"
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient([_Resp(status, text=body)], captured))
    ok, msg, sha = await publisher._merge_pr(7, {"merge_method": "merge"})
    assert ok is False and sha is None
    assert "merge 失敗" in msg
    assert str(status) in msg  # 明確標出狀態碼
    assert "secrettoken" not in msg  # token 遮蔽
    assert "***" in msg


@pytest.mark.asyncio
async def test_merge_network_exception_does_not_crash(monkeypatch, _configured):
    class _BoomClient:
        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put(self, *a, **k):
            raise RuntimeError("connect to secrettoken failed")

    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient())
    ok, msg, sha = await publisher._merge_pr(7, {"merge_method": "merge"})
    assert ok is False and sha is None
    assert "請求例外" in msg
    assert "secrettoken" not in msg


@pytest.mark.asyncio
async def test_merge_405_retries_then_gives_clear_error(monkeypatch, _configured):
    """剛建立的 PR 回 405（mergeable 計算中）→ 重試耗盡後仍回明確錯誤、不崩潰。"""
    monkeypatch.setattr(publisher, "_MERGE_POLL_TRIES", 3)
    monkeypatch.setattr(publisher, "_MERGE_POLL_DELAY", 0)

    captured = []
    responses = [_Resp(405, text="not mergeable secrettoken") for _ in range(3)]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient(responses, captured))

    ok, msg, sha = await publisher._merge_pr(7, {"merge_method": "merge"})
    assert ok is False and sha is None
    assert "merge 失敗" in msg and "405" in msg  # 重試耗盡後回明確錯誤
    assert "secrettoken" not in msg
    assert len(captured) == 3  # 確實重試到上限


@pytest.mark.asyncio
async def test_merge_405_then_success(monkeypatch, _configured):
    """405 後 mergeable 完成 → 第二次 200 成功合併。"""
    monkeypatch.setattr(publisher, "_MERGE_POLL_TRIES", 3)
    monkeypatch.setattr(publisher, "_MERGE_POLL_DELAY", 0)

    captured = []
    responses = [_Resp(405, text="calculating"), _Resp(200, payload={"sha": "cafef00d"})]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient(responses, captured))

    ok, msg, sha = await publisher._merge_pr(7, {"merge_method": "merge"})
    assert ok is True and sha == "cafef00d"


# --- 端到端 publish：merge 成功/失敗下，輸出全欄位不洩 token ------------


@pytest.mark.asyncio
async def test_publish_merge_success_to_dict_no_token(monkeypatch, _configured):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9", 9

    async def fake_merge(number, payload):
        return True, "已自動合併", "abc1234"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)

    res = await publisher.publish("/tmp", "s1", "需求", do_merge=True)
    d = res.to_dict()
    assert d["merged"] is True
    assert d["merge_sha"] == "abc1234"
    assert d["pr_url"].endswith("/pull/9")  # PR 連結存在
    # 全欄位字串化後不得出現 token
    assert "secrettoken" not in str(d)


@pytest.mark.asyncio
async def test_publish_merge_fail_outputs_no_token(monkeypatch, _configured):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9", 9

    async def fake_merge(number, payload):
        return False, "merge 失敗（409）：conflict ***", None

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)

    res = await publisher.publish("/tmp", "s1", "需求", do_merge=True)
    assert res.merged is False
    assert "merge 失敗" in res.detail
    assert "secrettoken" not in str(res.to_dict())


# --- 開關行為（任務 #1/#2 銜接點）：do_merge 預設不觸發 merge ----------


@pytest.mark.asyncio
async def test_default_no_merge_no_merge_call(monkeypatch, _configured):
    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9", 9

    calls = {"n": 0}

    async def fake_merge(number, payload):
        calls["n"] += 1
        return True, "", "x"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)

    res = await publisher.publish("/tmp", "s1", "需求")  # 預設 do_merge=False
    assert res.merged is False
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_merge_pr_number_none_skips_merge(monkeypatch, _configured):
    """PR 開成功但拿不到 number 時，安全略過 merge、不崩潰。"""

    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9", None  # number=None

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)

    res = await publisher.publish("/tmp", "s1", "需求", do_merge=True)
    assert res.ok and res.merged is False
    assert "略過自動合併" in res.detail
