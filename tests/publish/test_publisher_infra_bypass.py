"""基礎設施/帳務 CI 秒掛繞過合併（PUBLISH_BYPASS_INFRA_CI）。

情境：GitHub Actions 命中 spending limit 時，所有 job 在數秒內 conclusion=failure 且零步驟
執行——CI 紅其實是帳務問題、非程式碼失敗。本功能讓 autopilot 自動合併在此情境下繞過自設
「等 CI→紅就待人工」閘直接合併（main 未受保護、且發佈前 sandbox 測試已驗碼）。

涵蓋：
- is_infra_ci_failure：全部失敗 check 秒掛＝True；任一跑久／缺時間戳／無失敗＝False；門檻可調。
- _wait_for_ci：infra 特徵 + 開關開 → 回 "infra_fail"；開關關 → 仍 "fail"；有失敗 check 跑久 → "fail"。
- _merge_flow：infra_fail 不早退、照常嘗試合併 → MERGED，detail 註記已繞過。
"""

from __future__ import annotations

import pytest

from studio import config, publisher
from studio.publisher import MergeOutcome


def _run(
    name="lint",
    conclusion="failure",
    started="2026-06-14T08:09:21Z",
    completed="2026-06-14T08:09:24Z",
    status="completed",
):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started,
        "completed_at": completed,
    }


# --- is_infra_ci_failure（純函式）------------------------------------


def test_infra_all_failed_checks_are_fast():
    runs = [_run("lint"), _run("test"), _run("sandbox")]  # 皆 3 秒
    assert publisher.is_infra_ci_failure(runs) is True


def test_infra_false_when_a_failed_check_ran_long():
    # 一個失敗 check 跑了 5 分鐘（真實執行）→ 不視為基礎設施問題，保留待人工。
    runs = [_run("lint"), _run("test", completed="2026-06-14T08:14:21Z")]
    assert publisher.is_infra_ci_failure(runs) is False


def test_infra_false_when_no_failed_checks():
    assert publisher.is_infra_ci_failure([_run(conclusion="success")]) is False
    assert publisher.is_infra_ci_failure([]) is False
    assert publisher.is_infra_ci_failure(None) is False


def test_infra_false_when_timestamp_missing():
    # 拿不到執行時間＝無法證明秒掛 → 保守不繞過。
    assert publisher.is_infra_ci_failure([_run(started=None)]) is False
    assert publisher.is_infra_ci_failure([_run(completed=None)]) is False


def test_infra_only_considers_failed_checks():
    # 成功的 check 跑很久不影響判定；只看失敗 check 是否全部秒掛。
    runs = [
        _run("test", conclusion="success", completed="2026-06-14T08:20:00Z"),
        _run("lint"),  # 失敗、秒掛
    ]
    assert publisher.is_infra_ci_failure(runs) is True


def test_infra_threshold_is_configurable():
    runs = [_run("lint", completed="2026-06-14T08:09:40Z")]  # 19 秒
    assert publisher.is_infra_ci_failure(runs, max_seconds=25) is True
    assert publisher.is_infra_ci_failure(runs, max_seconds=10) is False


# --- _wait_for_ci ----------------------------------------------------


async def _no_sleep(_):
    return None


@pytest.mark.asyncio
async def test_wait_for_ci_infra_fail_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "PUBLISH_BYPASS_INFRA_CI", True)

    async def fake_fetch(sha):
        return ([_run("lint"), _run("test")], {})

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, detail = await publisher._wait_for_ci("sha", timeout=60, interval=10, sleep=_no_sleep)
    assert state == "infra_fail"
    assert "帳務" in detail or "基礎設施" in detail


@pytest.mark.asyncio
async def test_wait_for_ci_stays_fail_when_bypass_disabled(monkeypatch):
    monkeypatch.setattr(config, "PUBLISH_BYPASS_INFRA_CI", False)

    async def fake_fetch(sha):
        return ([_run("lint"), _run("test")], {})

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, _ = await publisher._wait_for_ci("sha", timeout=60, interval=10, sleep=_no_sleep)
    assert state == "fail"


@pytest.mark.asyncio
async def test_wait_for_ci_real_failure_not_bypassed(monkeypatch):
    # 有失敗 check 跑很久＝真實失敗 → 仍 "fail"，即使開關開著。
    monkeypatch.setattr(config, "PUBLISH_BYPASS_INFRA_CI", True)

    async def fake_fetch(sha):
        return ([_run("lint", completed="2026-06-14T08:14:21Z")], {})

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)
    state, _ = await publisher._wait_for_ci("sha", timeout=60, interval=10, sleep=_no_sleep)
    assert state == "fail"


# --- _merge_flow：infra_fail → 照常合併 ------------------------------


@pytest.mark.asyncio
async def test_merge_flow_infra_fail_proceeds_to_merge(monkeypatch):
    async def fake_status(number, **kw):
        return {"head": {"sha": "sha1"}, "mergeable_state": "clean"}

    async def fake_wait(sha, **kw):
        return ("infra_fail", "CI 失敗但研判為基礎設施/帳務（秒掛）")

    merge_calls = {"n": 0}

    async def fake_merge(number, payload):
        merge_calls["n"] += 1
        return (MergeOutcome.MERGED, "deadbeef", False)

    monkeypatch.setattr(publisher, "_get_pr_status", fake_status)
    monkeypatch.setattr(publisher, "_wait_for_ci", fake_wait)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)

    async def _sleep(_):
        return None

    outcome, detail = await publisher._merge_flow(
        7, {}, ci_timeout=60, ci_interval=1, retries=1, sleep=_sleep
    )
    assert outcome == MergeOutcome.MERGED
    assert merge_calls["n"] == 1  # infra_fail 沒早退、確實嘗試了合併
    assert "繞過" in detail
