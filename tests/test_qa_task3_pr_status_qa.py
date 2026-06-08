"""QA 驗收：任務#3 — 取得 PR 結構化狀態並把卡關原因分類為四類。

焦點＝『結構化狀態函式』本身：
  - classify_block_reason(pr, check_state)：把卡關原因分類為「CI 未過／缺審核／stale／衝突」。
  - _get_pr_status()：查 mergeable/mergeable_state/head sha，且 unknown→re-poll 契約（IO 層）。

對應驗收標準 1（明確指出原因類別，非原始 HTTP text）與架構決策（unknown 不可當終局）。
補強既有測試未覆蓋的 _get_pr_status re-poll IO 行為（mock httpx）。
"""

from __future__ import annotations

import httpx
import pytest

from studio import config, publisher
from studio.publisher import MergeOutcome


# === 卡關原因四分類：語意完整 + 兩兩可區分 ==================================


@pytest.mark.parametrize(
    "state,check_state,expected_cat,label_kw",
    [
        ("dirty", None, "conflict", "衝突"),
        ("dirty", "pass", "conflict", "衝突"),       # 衝突與 CI 無關，仍 conflict
        ("behind", None, "stale", "落後"),
        ("blocked", "fail", "ci_failed", "CI"),       # 必要檢查未過
        ("blocked", "pass", "needs_review", "審核"),  # CI 已過 → 真正原因是缺審核
        ("blocked", None, "needs_review", "審核"),    # 無 CI 資訊 → 預設缺審核
        ("unstable", "fail", "ci_failed", "CI"),
        ("unstable", "pass", "needs_review", "審核"),
        ("draft", "fail", "ci_failed", "CI"),
        ("draft", None, "needs_review", "審核"),
        ("clean", None, "mergeable", "可合併"),
        ("has_hooks", None, "mergeable", "可合併"),
        ("weird_value", None, "unknown", "未知"),
        ("unknown", None, "unknown", "未知"),
    ],
)
def test_classify_block_reason_matrix(state, check_state, expected_cat, label_kw):
    cat, label = publisher.classify_block_reason({"mergeable_state": state}, check_state)
    assert cat == expected_cat
    assert label_kw in label


def test_classify_block_reason_none_and_empty():
    assert publisher.classify_block_reason(None)[0] == "unknown"
    assert publisher.classify_block_reason({})[0] == "unknown"


def test_four_required_categories_distinct():
    """驗收 1 核心：CI未過／缺審核／stale／衝突 四類兩兩可區分（非糊成一團）。"""
    cats = {
        publisher.classify_block_reason({"mergeable_state": "blocked"}, "fail")[0],   # CI 未過
        publisher.classify_block_reason({"mergeable_state": "blocked"}, "pass")[0],   # 缺審核
        publisher.classify_block_reason({"mergeable_state": "behind"})[0],            # stale
        publisher.classify_block_reason({"mergeable_state": "dirty"})[0],             # 衝突
    }
    assert cats == {"ci_failed", "needs_review", "stale", "conflict"}
    assert len(cats) == 4


def test_block_reason_label_never_empty():
    """每個分類都要有非空人類可讀說明（不得回空字串 → 無 silent）。"""
    for state in ("dirty", "behind", "blocked", "unstable", "draft", "clean", "weird"):
        cat, label = publisher.classify_block_reason({"mergeable_state": state}, "fail")
        assert label and label.strip()


def test_classify_block_reason_vs_merge_state_consistent():
    """classify_block_reason 與 classify_merge_state 對同一狀態的判斷不互相矛盾。"""
    # dirty/behind → 兩者皆指向衝突類（CONFLICT / conflict|stale）
    assert publisher.classify_merge_state({"mergeable_state": "dirty"}) == MergeOutcome.CONFLICT
    assert publisher.classify_block_reason({"mergeable_state": "dirty"})[0] == "conflict"
    assert publisher.classify_merge_state({"mergeable_state": "behind"}) == MergeOutcome.CONFLICT
    assert publisher.classify_block_reason({"mergeable_state": "behind"})[0] == "stale"
    # blocked → BLOCKED；細分原因交給 block_reason
    assert publisher.classify_merge_state({"mergeable_state": "blocked"}) == MergeOutcome.BLOCKED


# === _get_pr_status：unknown re-poll 契約（IO，mock httpx）==================


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _patch_get_sequence(monkeypatch, responses, calls):
    """讓每次 httpx.AsyncClient().get 依序回傳 responses 中的下一個（用盡則重複最後一個）。"""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            i = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            r = responses[i]
            if isinstance(r, Exception):
                raise r
            return r

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


@pytest.fixture
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")


async def _no_sleep(_):
    return None


@pytest.mark.asyncio
async def test_get_pr_status_converged_no_repoll(monkeypatch, _cfg):
    """已收斂（clean + mergeable=True）→ 一次回，不 re-poll。"""
    calls = {"n": 0}
    _patch_get_sequence(
        monkeypatch,
        [_FakeResp(200, {"mergeable": True, "mergeable_state": "clean", "head": {"sha": "s"}})],
        calls,
    )
    data = await publisher._get_pr_status(7, sleep=_no_sleep, retries=5, interval=1)
    assert data["mergeable_state"] == "clean"
    assert calls["n"] == 1  # 不 re-poll
    assert publisher.classify_merge_state(data) == MergeOutcome.MERGED


@pytest.mark.asyncio
async def test_get_pr_status_unknown_then_converge(monkeypatch, _cfg):
    """先 unknown（GitHub 計算中）→ re-poll 直到收斂為 blocked。"""
    calls = {"n": 0}
    _patch_get_sequence(
        monkeypatch,
        [
            _FakeResp(200, {"mergeable": None, "mergeable_state": "unknown"}),
            _FakeResp(200, {"mergeable": None, "mergeable_state": "unknown"}),
            _FakeResp(200, {"mergeable": False, "mergeable_state": "blocked", "head": {"sha": "s"}}),
        ],
        calls,
    )
    data = await publisher._get_pr_status(7, sleep=_no_sleep, retries=5, interval=1)
    assert data["mergeable_state"] == "blocked"
    assert calls["n"] == 3  # 兩次 unknown 後第三次收斂
    assert publisher.classify_merge_state(data) == MergeOutcome.BLOCKED


@pytest.mark.asyncio
async def test_get_pr_status_mergeable_none_counts_as_unconverged(monkeypatch, _cfg):
    """mergeable=None（即使 state 看似已知）也視為未收斂 → re-poll。"""
    calls = {"n": 0}
    _patch_get_sequence(
        monkeypatch,
        [
            _FakeResp(200, {"mergeable": None, "mergeable_state": "clean"}),
            _FakeResp(200, {"mergeable": True, "mergeable_state": "clean", "head": {"sha": "s"}}),
        ],
        calls,
    )
    data = await publisher._get_pr_status(7, sleep=_no_sleep, retries=5, interval=1)
    assert data["mergeable"] is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_get_pr_status_never_converges_returns_last_and_classifies_error(monkeypatch, _cfg):
    """一直 unknown → 達上限回最後一次結果；classify 落 ERROR（不誤判可合併）。"""
    calls = {"n": 0}
    _patch_get_sequence(
        monkeypatch,
        [_FakeResp(200, {"mergeable": None, "mergeable_state": "unknown"})],
        calls,
    )
    data = await publisher._get_pr_status(7, sleep=_no_sleep, retries=3, interval=1)
    assert data is not None and data["mergeable_state"] == "unknown"
    assert calls["n"] == 4  # retries=3 → 共 4 次嘗試（有界，不無限）
    assert publisher.classify_merge_state(data) == MergeOutcome.ERROR


@pytest.mark.asyncio
async def test_get_pr_status_non_200_returns_none(monkeypatch, _cfg):
    """非 200（如 404/403）→ 回 None，不丟例外。"""
    calls = {"n": 0}
    _patch_get_sequence(monkeypatch, [_FakeResp(404, {})], calls)
    data = await publisher._get_pr_status(7, sleep=_no_sleep, retries=3, interval=1)
    assert data is None


@pytest.mark.asyncio
async def test_get_pr_status_network_exception_returns_none(monkeypatch, _cfg):
    """網路例外 → 回 None，不外拋（無 silent crash）。"""
    calls = {"n": 0}
    _patch_get_sequence(monkeypatch, [httpx.ConnectError("boom")], calls)
    data = await publisher._get_pr_status(7, sleep=_no_sleep, retries=3, interval=1)
    assert data is None


@pytest.mark.asyncio
async def test_get_pr_status_exposes_head_sha(monkeypatch, _cfg):
    """結構化狀態須帶 head sha（供 _wait_for_ci 鎖定要等的 commit）。"""
    calls = {"n": 0}
    _patch_get_sequence(
        monkeypatch,
        [_FakeResp(200, {"mergeable": True, "mergeable_state": "clean", "head": {"sha": "deadbeef"}})],
        calls,
    )
    data = await publisher._get_pr_status(7, sleep=_no_sleep, retries=2, interval=1)
    assert (data.get("head") or {}).get("sha") == "deadbeef"
