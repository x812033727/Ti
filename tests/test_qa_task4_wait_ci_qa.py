"""QA 驗收：任務#4 — 等待 CI 的輪詢函式（pass/fail/pending 分流 + timeout + 間隔）。

焦點＝『等待 CI』本身：
  - summarize_checks：把 check-runs 與 legacy status 歸併成 pass/fail/pending 三態。
  - _wait_for_ci：pending 續等／fail 早退／逾時主動回報（不無限等），含抖動韌性。
  - _fetch_ci：check-runs 分頁合併。

對應驗收標準 2（CI 仍在跑續等、CI 失敗明確停止不誤判）與 3（timeout 不無限阻塞）。
所有等待以 monkeypatch sleep 驗證，不真實阻塞。
"""

from __future__ import annotations

import httpx
import pytest

from studio import config, publisher

pytestmark = pytest.mark.asyncio


# === summarize_checks：三態歸併補強（各失敗 conclusion / status 交叉）========


@pytest.mark.parametrize(
    "conclusion", ["failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"]
)
async def test_summarize_each_fail_conclusion_is_fail(conclusion):
    runs = [{"name": "x", "status": "completed", "conclusion": conclusion}]
    state, _ = publisher.summarize_checks(runs, {})
    assert state == "fail"


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
async def test_summarize_benign_conclusions_are_pass(conclusion):
    runs = [{"name": "x", "status": "completed", "conclusion": conclusion}]
    state, _ = publisher.summarize_checks(runs, {})
    assert state == "pass"


async def test_summarize_runs_pass_but_status_pending_is_pending():
    """check-runs 全過，但 legacy status 仍 pending → 整體 pending（不可提早放行）。"""
    runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
    state, _ = publisher.summarize_checks(runs, {"state": "pending", "total_count": 1})
    assert state == "pending"


async def test_summarize_no_runs_but_status_success_is_pass():
    state, _ = publisher.summarize_checks([], {"state": "success", "total_count": 2})
    assert state == "pass"


async def test_summarize_no_runs_but_status_failure_is_fail():
    state, _ = publisher.summarize_checks([], {"state": "failure", "total_count": 1})
    assert state == "fail"


async def test_summarize_detail_never_empty():
    for runs, status in (([], {}), ([{"status": "completed", "conclusion": "success"}], {})):
        _, detail = publisher.summarize_checks(runs, status)
        assert detail and detail.strip()


# === _wait_for_ci：pending 續等 / fail 早退 / 逾時主動回報 ===================


def _patch_fetch_sequence(monkeypatch, seq, calls):
    """讓 _fetch_ci 依序回傳 seq（用盡則重複最後一個）。None 模擬查詢失敗。"""

    async def fake_fetch(head_sha):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(publisher, "_fetch_ci", fake_fetch)


def _counting_sleep():
    state = {"n": 0, "total": 0.0}

    async def sleep(s):
        state["n"] += 1
        state["total"] += s

    return sleep, state


async def test_wait_pending_then_pass(monkeypatch):
    """CI 仍在跑 → 持續等待；最終轉 pass → 回 pass（驗收 2：續等不誤判）。"""
    calls = {"n": 0}
    pend = ([{"name": "a", "status": "in_progress", "conclusion": None}], {})
    done = ([{"name": "a", "status": "completed", "conclusion": "success"}], {})
    _patch_fetch_sequence(monkeypatch, [pend, pend, done], calls)
    sleep, st = _counting_sleep()
    state, _ = await publisher._wait_for_ci("sha", timeout=100, interval=10, sleep=sleep)
    assert state == "pass"
    assert calls["n"] == 3  # 兩輪 pending 後第三輪 pass
    assert st["n"] == 2     # 等待了兩次


async def test_wait_fail_fast_does_not_wait_full_timeout(monkeypatch):
    """CI 失敗 → 立即回 fail，不等到 timeout，不誤判為成功（驗收 2）。"""
    calls = {"n": 0}
    fail = ([{"name": "a", "status": "completed", "conclusion": "failure"}], {})
    _patch_fetch_sequence(monkeypatch, [fail], calls)
    sleep, st = _counting_sleep()
    state, detail = await publisher._wait_for_ci("sha", timeout=600, interval=10, sleep=sleep)
    assert state == "fail"
    assert st["n"] == 0  # 一次就早退，完全沒等待


async def test_wait_timeout_reports_waited_and_last_state(monkeypatch):
    """一直 pending → 在 timeout 回 timeout，detail 含已等待秒數與最後狀態（不無限等）。"""
    calls = {"n": 0}
    pend = ([{"name": "build", "status": "in_progress", "conclusion": None}], {})
    _patch_fetch_sequence(monkeypatch, [pend], calls)
    sleep, st = _counting_sleep()
    state, detail = await publisher._wait_for_ci("sha", timeout=30, interval=10, sleep=sleep)
    assert state == "timeout"
    assert "逾時" in detail
    assert "已等待" in detail
    # 有界：interval=10、timeout=30 → 約 3~4 輪後逾時，不無限
    assert st["n"] <= 5


async def test_wait_interval_zero_does_not_hang(monkeypatch):
    """interval≤0 + 一直 pending → 不可無限迴圈，須在有限輪數回 timeout。"""
    calls = {"n": 0}
    pend = ([{"name": "a", "status": "queued", "conclusion": None}], {})
    _patch_fetch_sequence(monkeypatch, [pend], calls)
    sleep, st = _counting_sleep()
    state, _ = await publisher._wait_for_ci("sha", timeout=60, interval=0, sleep=sleep)
    assert state == "timeout"
    assert calls["n"] <= 2  # interval≤0 → 一輪 pending 即視為達 timeout


async def test_wait_no_ci_returns_pass_immediately(monkeypatch):
    """無 CI（空 runs + 空 status）→ pass，不空等到逾時。"""
    calls = {"n": 0}
    _patch_fetch_sequence(monkeypatch, [([], {})], calls)
    sleep, st = _counting_sleep()
    state, detail = await publisher._wait_for_ci("sha", timeout=600, interval=10, sleep=sleep)
    assert state == "pass"
    assert st["n"] == 0
    assert "無 CI" in detail


# === _wait_for_ci：抖動韌性（單次失敗不立即放棄，但不無限重試）==============


async def test_wait_tolerates_transient_fetch_error_then_recovers(monkeypatch):
    """單次查詢失敗（抖動）後恢復 → 不誤判 error，最終正常回 pass。"""
    calls = {"n": 0}
    done = ([{"name": "a", "status": "completed", "conclusion": "success"}], {})
    _patch_fetch_sequence(monkeypatch, [None, done], calls)  # 先失敗一次再成功
    sleep, st = _counting_sleep()
    state, _ = await publisher._wait_for_ci(
        "sha", timeout=600, interval=5, sleep=sleep, max_fetch_errors=3
    )
    assert state == "pass"


async def test_wait_consecutive_fetch_errors_give_up_as_error(monkeypatch):
    """連續查詢失敗達上限 → 回 error，不無限重試（不丟例外）。"""
    calls = {"n": 0}
    _patch_fetch_sequence(monkeypatch, [None], calls)  # 永遠失敗
    sleep, st = _counting_sleep()
    state, detail = await publisher._wait_for_ci(
        "sha", timeout=600, interval=5, sleep=sleep, max_fetch_errors=3
    )
    assert state == "error"
    assert calls["n"] == 3  # 達上限即放棄，有界
    assert detail and detail.strip()


# === _fetch_ci：check-runs 分頁合併（mock httpx）============================


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")


async def test_fetch_ci_paginates_check_runs(monkeypatch, _cfg):
    """check-runs 超過 100 → 翻頁抓齊，避免漏判未完成的 check。"""
    page1 = [{"name": f"c{i}", "status": "completed", "conclusion": "success"} for i in range(100)]
    page2 = [{"name": "c100", "status": "in_progress", "conclusion": None}]

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            if "/check-runs" in url:
                if params["page"] == 1:
                    return _FakeResp(200, {"total_count": 101, "check_runs": page1})
                return _FakeResp(200, {"total_count": 101, "check_runs": page2})
            return _FakeResp(200, {"state": "pending", "total_count": 1})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    runs, status = await publisher._fetch_ci("sha")
    assert len(runs) == 101  # 兩頁合併
    # 合併後第 101 個是未完成 → summarize 應判 pending（證明分頁有抓到，未漏判）
    assert publisher.summarize_checks(runs, status)[0] == "pending"


async def test_fetch_ci_non_200_returns_none(monkeypatch, _cfg):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            return _FakeResp(403, {})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await publisher._fetch_ci("sha") is None


async def test_fetch_ci_exception_returns_none(monkeypatch, _cfg):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await publisher._fetch_ci("sha") is None
