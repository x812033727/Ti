"""task #4：中介層可觀測性 hook 單元測試。

驗證 `run_with_retries` 的 metrics 累加（retry 次數／延遲）、observe 事件 sink，
以及與 idle/hard timeout（passthrough）正交——逾時走獨立 outcome、不被退避吞掉、
也不把退避路徑吞掉；observe sink 拋例外不得影響重試控制流。
全程零實際等待（注入 fake sleep）、零網路。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import llm_caller as lc


def _run(coro):
    return asyncio.run(coro)


class _RateLimit(lc.RateLimitSignal):
    pass


class _Timeout(Exception):
    """模擬 experts 的逾時例外（走 passthrough 獨立路徑）。"""


def _recorder():
    """收集 (event, fields) 的同步 observe sink。"""
    events: list[tuple[str, dict]] = []

    def observe(event, fields):
        events.append((event, dict(fields)))

    return events, observe


async def _noop_sleep(_seconds: float) -> None:
    """fake sleep：零實際等待。"""
    return None


async def _exhausted(snippet, partial):
    return f"FALLBACK:{partial}"


async def _api_error(snippet, partial):
    return f"APIERR:{snippet}"


# ── 成功：無重試 ────────────────────────────────────────────────────────────
def test_success_outcome_and_no_retry_events():
    events, observe = _recorder()
    metrics = lc.RetryMetrics()

    async def attempt():
        return "ok"

    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=3,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            sleep=_noop_sleep,
            metrics=metrics,
            observe=observe,
        )
    )
    assert out == "ok"
    assert metrics.outcome == "success"
    assert metrics.retries == 0
    assert metrics.total_delay == 0.0
    assert [e for e, _ in events] == [lc.EV_SUCCESS]


# ── 限流退避：metrics 累加 retry 次數與延遲，before_sleep 事件齊全 ──────────────
def test_retry_metrics_accumulate_and_before_sleep_events():
    events, observe = _recorder()
    metrics = lc.RetryMetrics()
    calls = {"n": 0}
    slept: list[float] = []

    async def attempt():
        calls["n"] += 1
        if calls["n"] <= 2:  # 前兩次撞限流，第三次成功
            raise _RateLimit(retry_after=None, snippet="rate_limit_error")
        return "done"

    async def sleep(s):
        slept.append(s)

    # 固定退避值，方便斷言上下界
    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=5,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            backoff=lambda ra, attempt: 1.5,
            sleep=sleep,
            metrics=metrics,
            observe=observe,
        )
    )
    assert out == "done"
    assert metrics.outcome == "success"
    assert metrics.retries == 2
    assert metrics.rate_limit_hits == 2
    assert metrics.total_delay == pytest.approx(3.0)
    assert metrics.last_delay == pytest.approx(1.5)
    assert slept == [1.5, 1.5]  # 每次退避都真的 sleep 了（注入 fake）
    # 事件序列：兩次 before_sleep retry + 最後 success
    kinds = [e for e, _ in events]
    assert kinds == [lc.EV_RETRY, lc.EV_RETRY, lc.EV_SUCCESS]
    # before_sleep fields 帶 attempt/delay/total_delay
    first = events[0][1]
    assert first["attempt"] == 0 and first["delay"] == 1.5
    assert first["max_retries"] == 5
    assert events[1][1]["total_delay"] == pytest.approx(3.0)


# ── 限流耗盡：outcome 與事件 ────────────────────────────────────────────────
def test_rate_limit_exhausted_outcome():
    events, observe = _recorder()
    metrics = lc.RetryMetrics()

    async def attempt():
        raise _RateLimit(retry_after=None, snippet="rate_limit_error", partial_text="half")

    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=2,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            backoff=lambda ra, a: 0.0,
            sleep=_noop_sleep,
            metrics=metrics,
            observe=observe,
        )
    )
    assert out == "FALLBACK:half"
    assert metrics.outcome == "rate_limit_exhausted"
    assert metrics.retries == 2  # 退避兩次後耗盡
    kinds = [e for e, _ in events]
    assert kinds == [lc.EV_RETRY, lc.EV_RETRY, lc.EV_RATE_LIMIT_EXHAUSTED]


# ── API 錯誤（非限流）：不重試，走 fallback ──────────────────────────────────
def test_api_error_outcome_no_retry():
    events, observe = _recorder()
    metrics = lc.RetryMetrics()

    async def attempt():
        raise lc.APIErrorSignal(kind="overloaded_error", snippet="overloaded_error")

    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=3,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            sleep=_noop_sleep,
            metrics=metrics,
            observe=observe,
        )
    )
    assert out.startswith("APIERR:")
    assert metrics.outcome == "api_error"
    assert metrics.retries == 0
    assert [e for e, _ in events] == [lc.EV_API_ERROR]


# ── 正交核心：逾時走獨立路徑，不被退避吞掉、也不計入退避 metrics ──────────────
def test_timeout_is_orthogonal_to_backoff():
    events, observe = _recorder()
    metrics = lc.RetryMetrics()
    seen = {"timeout_exc": None}

    async def attempt():
        raise _Timeout("idle timeout")

    async def on_passthrough(exc):
        seen["timeout_exc"] = exc
        return "ABORTED"

    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=5,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            sleep=_noop_sleep,
            passthrough=(_Timeout,),
            on_passthrough=on_passthrough,
            metrics=metrics,
            observe=observe,
        )
    )
    assert out == "ABORTED"
    assert isinstance(seen["timeout_exc"], _Timeout)
    # 逾時 outcome 獨立；退避 metrics 完全沒被觸碰（互不吞沒）
    assert metrics.outcome == "timeout"
    assert metrics.retries == 0
    assert metrics.total_delay == 0.0
    assert metrics.rate_limit_hits == 0
    kinds = [e for e, _ in events]
    assert kinds == [lc.EV_TIMEOUT]
    assert lc.EV_RETRY not in kinds  # 逾時絕不被當成限流退避


# ── 正交核心：先退避幾次後逾時——保留先前 retry 計數，但 outcome 仍是 timeout ──
def test_timeout_after_retries_preserves_counts_without_swallowing():
    events, observe = _recorder()
    metrics = lc.RetryMetrics()
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _RateLimit(retry_after=None, snippet="rate_limit_error")
        raise _Timeout("hard timeout")

    async def on_passthrough(exc):
        return "ABORTED"

    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=5,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            backoff=lambda ra, a: 2.0,
            sleep=_noop_sleep,
            passthrough=(_Timeout,),
            on_passthrough=on_passthrough,
            metrics=metrics,
            observe=observe,
        )
    )
    assert out == "ABORTED"
    # 先前兩次限流退避的 metrics 仍在；最終 outcome 為 timeout（不被退避吞掉、也不吞退避）
    assert metrics.retries == 2
    assert metrics.total_delay == pytest.approx(4.0)
    assert metrics.outcome == "timeout"
    kinds = [e for e, _ in events]
    assert kinds == [lc.EV_RETRY, lc.EV_RETRY, lc.EV_TIMEOUT]
    assert events[-1][1]["retries"] == 2  # 逾時事件帶當下 retry 計數


# ── 未知例外：re-raise，emit EV_UNKNOWN_ERROR ───────────────────────────────
def test_unknown_error_reraises_and_emits():
    events, observe = _recorder()
    metrics = lc.RetryMetrics()

    async def attempt():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _run(
            lc.run_with_retries(
                attempt,
                max_retries=3,
                on_rate_limit_exhausted=_exhausted,
                on_api_error=_api_error,
                sleep=_noop_sleep,
                metrics=metrics,
                observe=observe,
            )
        )
    assert metrics.outcome == "unknown_error"
    assert [e for e, _ in events] == [lc.EV_UNKNOWN_ERROR]


# ── 向後相容鐵則：observe sink 拋例外不得破壞重試控制流 ──────────────────────
def test_observe_sink_exception_does_not_break_control_flow():
    metrics = lc.RetryMetrics()
    calls = {"n": 0}

    def bad_observe(event, fields):
        raise RuntimeError("sink down")

    async def attempt():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RateLimit(retry_after=None, snippet="rate_limit_error")
        return "recovered"

    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=3,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            backoff=lambda ra, a: 0.1,
            sleep=_noop_sleep,
            metrics=metrics,
            observe=bad_observe,  # 每次 emit 都拋例外
        )
    )
    # sink 全程拋例外，但重試照常完成、metrics 照常累加
    assert out == "recovered"
    assert metrics.retries == 1
    assert metrics.outcome == "success"


# ── metrics 預設可選：不傳 metrics/observe 完全向後相容 ─────────────────────
def test_metrics_and_observe_are_optional():
    async def attempt():
        return "ok"

    out = _run(
        lc.run_with_retries(
            attempt,
            max_retries=1,
            on_rate_limit_exhausted=_exhausted,
            on_api_error=_api_error,
            sleep=_noop_sleep,
        )
    )
    assert out == "ok"


def test_retry_metrics_to_dict_shape():
    m = lc.RetryMetrics(retries=2, total_delay=3.0, last_delay=1.5, rate_limit_hits=2, outcome="success")
    d = m.to_dict()
    assert d == {
        "retries": 2,
        "total_delay": 3.0,
        "last_delay": 1.5,
        "rate_limit_hits": 2,
        "outcome": "success",
    }
