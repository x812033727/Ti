"""任務 #1：補強合併等待與回報，消除 silent failed。

涵蓋：
- 純函式 classify_merge_state / summarize_checks（含空 checks、混合交叉案例）。
- _wait_for_ci：pass / fail 早退 / pending→逾時（不真實等待，monkeypatch sleep）。
- _merge_flow：MERGED / CI_FAILED / BLOCKED / CONFLICT / TIMEOUT / ERROR 六結局可區分。
- behind（stale）→ update-branch 後重試；409 重試上限。
"""

from __future__ import annotations

import pytest

from studio import config, publisher
from studio.publisher import MergeOutcome

# --- 純函式：classify_merge_state ------------------------------------


@pytest.mark.parametrize(
    "state,expected",
    [
        ("clean", MergeOutcome.MERGED),
        ("has_hooks", MergeOutcome.MERGED),
        ("behind", MergeOutcome.CONFLICT),
        ("dirty", MergeOutcome.CONFLICT),
        ("blocked", MergeOutcome.BLOCKED),
        ("unstable", MergeOutcome.BLOCKED),
        ("draft", MergeOutcome.BLOCKED),
        ("unknown", MergeOutcome.ERROR),
    ],
)
def test_classify_merge_state_known(state, expected):
    assert publisher.classify_merge_state({"mergeable_state": state}) == expected


def test_classify_merge_state_unknown_value_falls_back_to_error():
    # 非已知列舉值絕不默默當 clean，一律 ERROR
    assert publisher.classify_merge_state({"mergeable_state": "wat"}) == MergeOutcome.ERROR
    assert publisher.classify_merge_state({}) == MergeOutcome.ERROR
    assert publisher.classify_merge_state(None) == MergeOutcome.ERROR


# --- 純函式：classify_block_reason（四類卡關原因）-------------------


def test_block_reason_conflict():
    cat, label = publisher.classify_block_reason({"mergeable_state": "dirty"})
    assert cat == "conflict" and "衝突" in label


def test_block_reason_stale():
    cat, label = publisher.classify_block_reason({"mergeable_state": "behind"})
    assert cat == "stale" and "落後" in label


def test_block_reason_blocked_with_ci_fail_is_ci_failed():
    """blocked + CI 失敗 → 區分為「CI 未過」。"""
    cat, label = publisher.classify_block_reason({"mergeable_state": "blocked"}, "fail")
    assert cat == "ci_failed" and "CI" in label


def test_block_reason_blocked_with_ci_pass_is_needs_review():
    """blocked + CI 已過 → 區分為「缺審核」（這是 405 含糊文字最常見的真實原因）。"""
    cat, label = publisher.classify_block_reason({"mergeable_state": "blocked"}, "pass")
    assert cat == "needs_review" and "審核" in label


def test_block_reason_unstable_no_ci_state_defaults_needs_review():
    cat, _ = publisher.classify_block_reason({"mergeable_state": "unstable"})
    assert cat == "needs_review"


def test_block_reason_mergeable_and_unknown():
    assert publisher.classify_block_reason({"mergeable_state": "clean"})[0] == "mergeable"
    assert publisher.classify_block_reason({"mergeable_state": "wat"})[0] == "unknown"
    assert publisher.classify_block_reason(None)[0] == "unknown"


def test_block_reason_four_categories_are_distinct():
    """驗收 1：CI未過／缺審核／stale／衝突 四類必須兩兩可區分。"""
    cats = {
        publisher.classify_block_reason({"mergeable_state": "blocked"}, "fail")[0],
        publisher.classify_block_reason({"mergeable_state": "blocked"}, "pass")[0],
        publisher.classify_block_reason({"mergeable_state": "behind"})[0],
        publisher.classify_block_reason({"mergeable_state": "dirty"})[0],
    }
    assert cats == {"ci_failed", "needs_review", "stale", "conflict"}


# --- 純函式：summarize_checks ----------------------------------------


def test_summarize_checks_empty_is_pass_no_ci():
    state, detail = publisher.summarize_checks([], {})
    assert state == "pass" and "無 CI" in detail


def test_summarize_checks_all_pass():
    runs = [
        {"name": "a", "status": "completed", "conclusion": "success"},
        {"name": "b", "status": "completed", "conclusion": "skipped"},
    ]
    state, _ = publisher.summarize_checks(runs, {"state": "success", "total_count": 1})
    assert state == "pass"


def test_summarize_checks_any_fail_is_fail():
    runs = [
        {"name": "a", "status": "completed", "conclusion": "success"},
        {"name": "b", "status": "completed", "conclusion": "failure"},
    ]
    state, detail = publisher.summarize_checks(runs, {})
    assert state == "fail" and "b" in detail


def test_summarize_checks_pending():
    runs = [{"name": "a", "status": "in_progress", "conclusion": None}]
    state, _ = publisher.summarize_checks(runs, {})
    assert state == "pending"


def test_summarize_checks_runs_pass_but_legacy_status_fail():
    """交叉案例：check-runs 全過，但 legacy status 為 failure → 整體 fail。"""
    runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
    state, _ = publisher.summarize_checks(runs, {"state": "failure", "total_count": 1})
    assert state == "fail"


def test_summarize_checks_mixed_fail_beats_pending():
    """混合：有 fail 也有 pending → fail 優先（fail-fast）。"""
    runs = [
        {"name": "a", "status": "in_progress", "conclusion": None},
        {"name": "b", "status": "completed", "conclusion": "timed_out"},
    ]
    state, _ = publisher.summarize_checks(runs, {})
    assert state == "fail"


# --- _wait_for_ci ----------------------------------------------------


@pytest.fixture
def _no_sleep():
    async def fake_sleep(_):
        return None

    return fake_sleep


@pytest.mark.asyncio
async def test_wait_for_ci_pass(monkeypatch, _no_sleep):
    async def fake_fetch(sha):
        return ([{"name": "a", "status": "completed", "conclusion": "success"}], {})

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, _ = await publisher._wait_for_ci("sha", timeout=60, interval=10, sleep=_no_sleep)
    assert state == "pass"


@pytest.mark.asyncio
async def test_wait_for_ci_fail_fast(monkeypatch, _no_sleep):
    async def fake_fetch(sha):
        return ([{"name": "a", "status": "completed", "conclusion": "failure"}], {})

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, detail = await publisher._wait_for_ci("sha", timeout=60, interval=10, sleep=_no_sleep)
    assert state == "fail"


@pytest.mark.asyncio
async def test_wait_for_ci_pending_then_timeout(monkeypatch):
    """一直 pending → 逾時早退，不無限等；sleep 被 monkeypatch 不真實等待。"""
    slept = {"n": 0}

    async def fake_sleep(_):
        slept["n"] += 1

    async def fake_fetch(sha):
        return ([{"name": "a", "status": "in_progress", "conclusion": None}], {})

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, detail = await publisher._wait_for_ci("sha", timeout=30, interval=10, sleep=fake_sleep)
    assert state == "timeout" and "逾時" in detail
    assert slept["n"] == 3  # 10,20,30 後逾時


@pytest.mark.asyncio
async def test_wait_for_ci_persistent_fetch_error_bounded(monkeypatch, _no_sleep):
    """持續查詢失敗 → 連續上限後回 error（有界，不無限重試）。"""
    calls = {"n": 0}

    async def fake_fetch(sha):
        calls["n"] += 1
        return None

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, detail = await publisher._wait_for_ci(
        "sha", timeout=600, interval=10, sleep=_no_sleep, max_fetch_errors=3
    )
    assert state == "error" and "連續失敗" in detail
    assert calls["n"] == 3  # 連續 3 次失敗即放棄，未因大 timeout 而暴衝


@pytest.mark.asyncio
async def test_wait_for_ci_transient_error_then_pass(monkeypatch, _no_sleep):
    """單次查詢抖動後恢復 → 不誤判 error，續等到 pass。"""
    seq = [None, ([{"name": "a", "status": "in_progress", "conclusion": None}], {}),
           ([{"name": "a", "status": "completed", "conclusion": "success"}], {})]
    idx = {"i": 0}

    async def fake_fetch(sha):
        r = seq[idx["i"]]
        idx["i"] += 1
        return r

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, _ = await publisher._wait_for_ci("sha", timeout=600, interval=10, sleep=_no_sleep)
    assert state == "pass"  # 第一次 None 被容忍，最終 pass


@pytest.mark.asyncio
async def test_wait_for_ci_zero_interval_no_infinite_loop(monkeypatch, _no_sleep):
    """interval<=0 且持續 pending → 一輪即視為逾時，不無限迴圈。"""
    async def fake_fetch(sha):
        return ([{"name": "a", "status": "in_progress", "conclusion": None}], {})

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, _ = await publisher._wait_for_ci("sha", timeout=30, interval=0, sleep=_no_sleep)
    assert state == "timeout"


# --- _merge_flow 六結局 ----------------------------------------------


@pytest.fixture
def _patch_flow(monkeypatch):
    """提供可調整的 _get_pr_status / _wait_for_ci / _merge_pr / _update_branch stubs。"""

    state = {
        "pr": {"head": {"sha": "sha1"}, "mergeable": True, "mergeable_state": "clean"},
        "ci": ("pass", "ok"),
        "merge": (MergeOutcome.MERGED, "deadbeef", False),
        "updates": 0,
        "merge_calls": 0,
    }

    async def fake_status(number, **kw):
        return state["pr"]

    async def fake_wait(sha, **kw):
        return state["ci"]

    async def fake_merge(number, payload):
        state["merge_calls"] += 1
        return state["merge"]

    async def fake_update(number):
        state["updates"] += 1
        return True

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(publisher, "_get_pr_status", fake_status)
    monkeypatch.setattr(publisher, "_wait_for_ci", fake_wait)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    monkeypatch.setattr(publisher, "_update_branch", fake_update)
    state["sleep"] = fake_sleep
    return state


async def _run_flow(state, retries=3):
    return await publisher._merge_flow(
        7, {}, ci_timeout=60, ci_interval=1, retries=retries, sleep=state["sleep"]
    )


@pytest.mark.asyncio
async def test_flow_merged(_patch_flow):
    outcome, _ = await _run_flow(_patch_flow)
    assert outcome == MergeOutcome.MERGED


@pytest.mark.asyncio
async def test_flow_ci_failed_does_not_merge(_patch_flow):
    _patch_flow["ci"] = ("fail", "CI 失敗：lint")
    outcome, detail = await _run_flow(_patch_flow)
    assert outcome == MergeOutcome.CI_FAILED
    assert _patch_flow["merge_calls"] == 0  # CI 未過絕不嘗試合併


@pytest.mark.asyncio
async def test_flow_timeout(_patch_flow):
    _patch_flow["ci"] = ("timeout", "等待 CI 逾時（已等待 60s）")
    outcome, detail = await _run_flow(_patch_flow)
    assert outcome == MergeOutcome.TIMEOUT and "逾時" in detail
    assert _patch_flow["merge_calls"] == 0


@pytest.mark.asyncio
async def test_flow_blocked(_patch_flow):
    # CI 過了卻被擋（缺審核／保護規則）→ BLOCKED；結構化狀態 blocked 精準分類
    _patch_flow["pr"] = {"head": {"sha": "s"}, "mergeable": True, "mergeable_state": "blocked"}
    _patch_flow["merge"] = (MergeOutcome.BLOCKED, "不可合併／受保護（405）", False)
    outcome, detail = await _run_flow(_patch_flow)
    assert outcome == MergeOutcome.BLOCKED
    # 驗收 1：CI 已過卻 blocked → detail 明確指出「缺審核」，不再只回原始 405 text
    assert "審核" in detail


@pytest.mark.asyncio
async def test_flow_conflict_dirty(_patch_flow):
    _patch_flow["pr"] = {"head": {"sha": "s"}, "mergeable": False, "mergeable_state": "dirty"}
    _patch_flow["merge"] = (MergeOutcome.BLOCKED, "不可合併（405）", False)
    outcome, _ = await _run_flow(_patch_flow)
    # 結構化狀態 dirty → 精準分類為 CONFLICT（覆蓋 405 粗分類）
    assert outcome == MergeOutcome.CONFLICT


@pytest.mark.asyncio
async def test_flow_error_on_status_failure(_patch_flow, monkeypatch):
    async def none_status(number, **kw):
        return None

    monkeypatch.setattr(publisher, "_get_pr_status", none_status)
    outcome, detail = await _run_flow(_patch_flow)
    assert outcome == MergeOutcome.ERROR


@pytest.mark.asyncio
async def test_flow_behind_retries_with_update_branch(_patch_flow):
    """behind（stale）→ 409 可重試：呼叫 update-branch 後重試，最終合併成功。"""
    calls = {"n": 0}

    async def flaky_merge(number, payload):
        calls["n"] += 1
        if calls["n"] < 3:
            return MergeOutcome.CONFLICT, "Base branch was modified（409）", True
        return MergeOutcome.MERGED, "sha-final", False

    import studio.publisher as p

    p._merge_pr = flaky_merge  # 直接覆蓋（fixture 已 monkeypatch，測後自動還原）
    outcome, _ = await _run_flow(_patch_flow, retries=3)
    assert outcome == MergeOutcome.MERGED
    assert calls["n"] == 3
    assert _patch_flow["updates"] == 2  # 前兩次失敗各 update-branch 一次


@pytest.mark.asyncio
async def test_flow_retry_exhausted(_patch_flow):
    """可重試錯誤一直失敗 → 達上限後放棄並回報（不無限重試）。"""
    _patch_flow["merge"] = (MergeOutcome.CONFLICT, "Base branch was modified（409）", True)
    outcome, detail = await _run_flow(_patch_flow, retries=2)
    assert outcome == MergeOutcome.CONFLICT
    assert "重試上限" in detail
    # retries=2 → 共嘗試 3 次合併
    assert _patch_flow["merge_calls"] == 3


# --- config 連動 -----------------------------------------------------


def test_config_has_ci_merge_settings():
    assert isinstance(config.PUBLISH_CI_TIMEOUT, int)
    assert isinstance(config.PUBLISH_CI_INTERVAL, int)
    assert isinstance(config.PUBLISH_MERGE_RETRIES, int)


def test_config_reload_picks_up_ci_settings(monkeypatch):
    monkeypatch.setenv("TI_PUBLISH_CI_TIMEOUT", "123")
    monkeypatch.setenv("TI_PUBLISH_CI_INTERVAL", "7")
    monkeypatch.setenv("TI_PUBLISH_MERGE_RETRIES", "5")
    try:
        config.reload()
        assert config.PUBLISH_CI_TIMEOUT == 123
        assert config.PUBLISH_CI_INTERVAL == 7
        assert config.PUBLISH_MERGE_RETRIES == 5
    finally:
        monkeypatch.delenv("TI_PUBLISH_CI_TIMEOUT", raising=False)
        monkeypatch.delenv("TI_PUBLISH_CI_INTERVAL", raising=False)
        monkeypatch.delenv("TI_PUBLISH_MERGE_RETRIES", raising=False)
        config.reload()
