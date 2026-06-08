"""QA 驗收：任務#5 — _merge_pr 對 stale／`Base branch was modified`（409）有限次重試＋backoff。

焦點：
  - _merge_pr：HTTP 狀態碼 → (outcome, detail, retryable) 完整分流矩陣（不丟例外）。
  - _merge_flow：可重試錯誤有限次重試；每次重試之間以指數 backoff 退避；
                 behind（stale）才 update-branch 實修，其餘暫時性錯誤純退避；超限放棄並回報。
  - _backoff：指數成長且封頂。

對應驗收標準 4（stale/409 有限次重試＋backoff，超限放棄回報）與 5（皆不丟例外、有 detail）。
"""

from __future__ import annotations

import httpx
import pytest

from studio import config, publisher
from studio.publisher import MergeOutcome

# === _backoff 純函式：指數 + 封頂 =========================================


def test_backoff_exponential_from_attempt_zero():
    assert publisher._backoff(0, 5) == 5
    assert publisher._backoff(1, 5) == 10
    assert publisher._backoff(2, 5) == 20
    assert publisher._backoff(3, 5) == 40


def test_backoff_capped_at_60():
    assert publisher._backoff(10, 10) == 60.0
    assert publisher._backoff(100, 1) == 60.0


def test_backoff_is_monotonic_nondecreasing():
    base = 3
    vals = [publisher._backoff(a, base) for a in range(8)]
    assert all(b >= a for a, b in zip(vals, vals[1:], strict=False))


# === _merge_pr：完整分流矩陣（mock httpx PUT）=============================


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


@pytest.fixture
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")


@pytest.mark.asyncio
async def test_merge_pr_200_merged_not_retryable(monkeypatch, _cfg):
    _patch_put(monkeypatch, resp=_FakeResp(200, {"sha": "abc"}))
    outcome, info, retryable = await publisher._merge_pr(7, {})
    assert outcome is MergeOutcome.MERGED and info == "abc" and retryable is False


@pytest.mark.asyncio
async def test_merge_pr_409_conflict_is_retryable(monkeypatch, _cfg):
    """409 / Base branch was modified → CONFLICT 且可重試（任務 #5 核心）。"""
    _patch_put(monkeypatch, resp=_FakeResp(409, text="Base branch was modified"))
    outcome, detail, retryable = await publisher._merge_pr(7, {})
    assert outcome is MergeOutcome.CONFLICT and retryable is True
    assert "409" in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [405, 422])
async def test_merge_pr_405_422_blocked_not_retryable(monkeypatch, _cfg, code):
    """405/422（受保護／不符規則）→ BLOCKED 且不可重試（不進重試迴圈白等）。"""
    _patch_put(monkeypatch, resp=_FakeResp(code, text="protected"))
    outcome, detail, retryable = await publisher._merge_pr(7, {})
    assert outcome is MergeOutcome.BLOCKED and retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [500, 502, 503])
async def test_merge_pr_5xx_error_is_retryable(monkeypatch, _cfg, code):
    """5xx（GitHub 暫時性）→ ERROR 且可重試。"""
    _patch_put(monkeypatch, resp=_FakeResp(code, text="server error"))
    outcome, detail, retryable = await publisher._merge_pr(7, {})
    assert outcome is MergeOutcome.ERROR and retryable is True


@pytest.mark.asyncio
async def test_merge_pr_other_4xx_error_not_retryable(monkeypatch, _cfg):
    """其他 4xx（如 403）→ ERROR 且不可重試（不白等）。"""
    _patch_put(monkeypatch, resp=_FakeResp(403, text="forbidden"))
    outcome, detail, retryable = await publisher._merge_pr(7, {})
    assert outcome is MergeOutcome.ERROR and retryable is False


@pytest.mark.asyncio
async def test_merge_pr_network_exception_no_raise(monkeypatch, _cfg):
    """網路例外 → ERROR 可重試，且不外拋（無 silent crash）。"""
    _patch_put(monkeypatch, exc=httpx.ConnectError("boom"))
    outcome, detail, retryable = await publisher._merge_pr(7, {})
    assert outcome is MergeOutcome.ERROR and retryable is True
    assert detail and detail.strip()


# === _merge_flow：重試 + backoff + update-branch 分流 ======================


@pytest.fixture
def _flow(monkeypatch):
    """mock 掉 flow 內部 IO；status/ci 一次收斂且不耗 sleep，使 sleep 只反映 backoff。"""
    st = {
        "mergeable_state": "behind",
        "merge_seq": [(MergeOutcome.MERGED, "sha", False)],
        "merge_calls": 0,
        "update_calls": 0,
        "sleeps": [],
    }

    async def fake_status(number, **kw):
        return {"head": {"sha": "s"}, "mergeable": False, "mergeable_state": st["mergeable_state"]}

    async def fake_wait(sha, **kw):
        return "pass", "CI 全過"

    async def fake_merge(number, payload):
        i = min(st["merge_calls"], len(st["merge_seq"]) - 1)
        st["merge_calls"] += 1
        return st["merge_seq"][i]

    async def fake_update(number):
        st["update_calls"] += 1
        return True

    async def fake_sleep(s):
        st["sleeps"].append(s)

    monkeypatch.setattr(publisher, "_get_pr_status", fake_status)
    monkeypatch.setattr(publisher, "_wait_for_ci", fake_wait)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    monkeypatch.setattr(publisher, "_update_branch", fake_update)
    st["sleep"] = fake_sleep
    return st


async def _run(st, retries=3, ci_interval=5):
    return await publisher._merge_flow(
        7, {}, ci_timeout=60, ci_interval=ci_interval, retries=retries, sleep=st["sleep"]
    )


@pytest.mark.asyncio
async def test_behind_retry_backoff_is_exponential(_flow):
    """behind 連續 409 兩次後成功 → 兩次重試之間的退避為指數 backoff（attempt 0,1）。"""
    _flow["merge_seq"] = [
        (MergeOutcome.CONFLICT, "409", True),
        (MergeOutcome.CONFLICT, "409", True),
        (MergeOutcome.MERGED, "sha", False),
    ]
    outcome, _ = await _run(_flow, retries=3, ci_interval=5)
    assert outcome is MergeOutcome.MERGED
    assert _flow["merge_calls"] == 3
    # 兩次退避 = _backoff(0,5)=5, _backoff(1,5)=10 → 指數成長且有 backoff
    assert _flow["sleeps"] == [5, 10]


@pytest.mark.asyncio
async def test_behind_calls_update_branch_each_retry(_flow):
    """stale（behind）每次重試前都 update-branch 實修分支（非僅 backoff 假修）。"""
    _flow["mergeable_state"] = "behind"
    _flow["merge_seq"] = [
        (MergeOutcome.CONFLICT, "409", True),
        (MergeOutcome.MERGED, "sha", False),
    ]
    await _run(_flow, retries=3)
    assert _flow["update_calls"] == 1  # 一次失敗 → 一次 update-branch


@pytest.mark.asyncio
async def test_non_behind_retry_skips_update_branch(_flow):
    """非 behind 的暫時性錯誤（5xx）→ 純 backoff 重試，不做多餘 update-branch。"""
    _flow["mergeable_state"] = "clean"  # 狀態不是 behind
    _flow["merge_seq"] = [
        (MergeOutcome.ERROR, "500", True),
        (MergeOutcome.MERGED, "sha", False),
    ]
    outcome, _ = await _run(_flow, retries=3)
    assert outcome is MergeOutcome.MERGED
    assert _flow["update_calls"] == 0  # 不 update-branch
    assert _flow["sleeps"] == [5]      # 仍有一次 backoff 退避


@pytest.mark.asyncio
async def test_retry_finite_then_give_up_with_detail(_flow):
    """可重試錯誤一直失敗 → 達上限放棄，回報含「重試上限」，不無限重試（驗收 4）。"""
    _flow["mergeable_state"] = "behind"
    _flow["merge_seq"] = [(MergeOutcome.CONFLICT, "Base branch was modified（409）", True)]
    outcome, detail = await _run(_flow, retries=2)
    assert outcome is MergeOutcome.CONFLICT
    assert "重試上限" in detail
    assert _flow["merge_calls"] == 3   # retries=2 → 1 + 2 重試 = 3 次
    assert len(_flow["sleeps"]) == 2   # 兩次重試前各退避一次（有界）


@pytest.mark.asyncio
async def test_blocked_405_never_retries(_flow):
    """405（不可重試）只嘗試一次，完全不退避、不 update-branch（不白等）。"""
    _flow["mergeable_state"] = "blocked"
    _flow["merge_seq"] = [(MergeOutcome.BLOCKED, "405 受保護", False)]
    outcome, detail = await _run(_flow, retries=3)
    assert outcome is MergeOutcome.BLOCKED
    assert _flow["merge_calls"] == 1
    assert _flow["update_calls"] == 0
    assert _flow["sleeps"] == []  # 完全沒退避


@pytest.mark.asyncio
async def test_5xx_exhausted_returns_error_with_detail(_flow):
    """5xx 一直失敗 → 達上限回 ERROR 且有 detail（不 silent、不丟例外）。"""
    _flow["mergeable_state"] = "clean"
    _flow["merge_seq"] = [(MergeOutcome.ERROR, "GitHub 伺服器錯誤（500）", True)]
    outcome, detail = await _run(_flow, retries=2)
    assert outcome is MergeOutcome.ERROR
    assert detail and detail.strip()
    assert _flow["merge_calls"] == 3


@pytest.mark.asyncio
async def test_retries_count_from_config(monkeypatch, _flow):
    """重試次數可由設定調整：retries 參數變大 → 嘗試次數隨之增加（驗收 6 連動）。"""
    _flow["mergeable_state"] = "behind"
    _flow["merge_seq"] = [(MergeOutcome.CONFLICT, "409", True)]
    await _run(_flow, retries=5)
    assert _flow["merge_calls"] == 6  # 1 + 5 重試
