"""核心 LLM 韌性中介層 `studio/llm_caller.py` 單元測試。

驗證 provider 無關契約：分類器（含反向黑樣本）、退避（retry-after 優先＋夾 cap＋指數）、
重試骨幹 `run_with_retries`（限流退避重試→成功／耗盡 fallback、API 錯誤不重試、逾時等
passthrough 獨立路徑、未知例外 re-raise）。全程不需真 SDK、不連線、注入 fake sleep。
"""

from __future__ import annotations

import pytest

from studio import llm_caller as lc

_RATE_LIMIT_JSON = '{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
_OVERLOADED_JSON = '{"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}'


# --- classify_api_text：錨定判別 + 反向黑樣本 --------------------------


def test_classify_rate_limit_error_json():
    assert lc.classify_api_text(_RATE_LIMIT_JSON) == ("rate_limit", None)


def test_classify_reads_retry_after():
    assert lc.classify_api_text(_RATE_LIMIT_JSON + " retry-after: 7") == ("rate_limit", 7.0)


def test_classify_status_429():
    assert lc.classify_api_text("API Error: status code 429") == ("rate_limit", None)


def test_classify_overloaded_is_overloaded_retryable():
    # 529／overloaded 改分到可退避的 overloaded 類（不再是 api_error），走純指數退避重試。
    assert lc.classify_api_text(_OVERLOADED_JSON) == ("overloaded", "overloaded_error")


def test_classify_status_529_is_overloaded():
    assert lc.classify_api_text("API Error: status code 529 overloaded") == (
        "overloaded",
        "HTTP 529",
    )


def test_classify_http_503_is_api_error():
    # 503（非 529）仍屬不重試的 api_error，確保只有 529 被分到過載退避路徑。
    assert lc.classify_api_text("API Error: HTTP 503") == ("api_error", "HTTP 503")


@pytest.mark.parametrize(
    "text",
    [
        "回應 @架構師: 同意。我們之前撞到 rate limit error 與 429，但已修好。",
        "建議對 overloaded error 做退避重試。",
        "這支測試有 429 個案例，error 訊息要更清楚。",
        "",
    ],
)
def test_classify_normal_speech_not_misclassified(text):
    assert lc.classify_api_text(text) is None


# --- _SSE_ERROR_TYPE_TO_STATUS：對照表 + SSE 防線（Issue #1258）---------


def test_sse_error_status_mapping_complete():
    # 對照表齊全：429／529 為核心分流碼，其餘常見型別亦有對應。
    assert lc.sse_error_status("rate_limit_error") == 429
    assert lc.sse_error_status("overloaded_error") == 529
    assert lc.sse_error_status("api_error") == 500
    assert lc.sse_error_status("authentication_error") == 401
    assert lc.sse_error_status("not_found_error") == 404
    assert lc.sse_error_status("invalid_request_error") == 400
    assert lc.sse_error_status("unknown_kind") is None
    assert lc.sse_error_status(None) is None


def test_is_rate_limit_type_only_429():
    assert lc.is_rate_limit_type("rate_limit_error") is True
    assert lc.is_rate_limit_type("overloaded_error") is False
    assert lc.is_rate_limit_type("unknown_kind") is False


def test_classify_sse_error_rate_limit_signal():
    sig = lc.classify_sse_error("rate_limit_error", "slow down retry-after: 9", partial_text="半句")
    assert isinstance(sig, lc.RateLimitSignal)
    assert sig.retry_after == 9.0
    assert sig.partial_text == "半句"


def test_classify_sse_error_overloaded_is_retryable_overloaded():
    # 529 過載：SSE 防線判為可退避重試的 OverloadedSignal（純指數退避），非直接 fallback。
    sig = lc.classify_sse_error("overloaded_error", "model overloaded")
    assert isinstance(sig, lc.OverloadedSignal)
    assert not isinstance(sig, lc.APIErrorSignal)
    assert sig.kind == "overloaded_error"


def test_classify_sse_error_unknown_is_conservative_api_error():
    # 未知型別保守視為 api_error（不可誤判為可無限退避的限流）。
    sig = lc.classify_sse_error("mystery_error")
    assert isinstance(sig, lc.APIErrorSignal)


def test_sse_overloaded_ignores_bogus_status_200():
    # Issue #1258 核心場景：HTTP 200 但 SSE error 為 overloaded → 以型別判為可重試的 overloaded
    # （529），不被 status 200 騙成正常文字／不重試。
    text = '{"type":"error","error":{"type":"overloaded_error"}} status code 200'
    assert lc.classify_api_text(text) == ("overloaded", "overloaded_error")


def test_sse_rate_limit_ignores_bogus_status_200():
    text = '{"type":"error","error":{"type":"rate_limit_error"}} status code 200'
    assert lc.classify_api_text(text) == ("rate_limit", None)


class _FakeAPIStatusError(Exception):
    """模擬 SDK 把 SSE error 包成 status_code=200、真實型別藏在 .body 的形態。"""

    def __init__(self, status_code, body):
        super().__init__(f"APIStatusError status_code={status_code}")
        self.status_code = status_code
        self.body = body


def test_classify_failure_sse_body_overloaded_ignores_status_200():
    exc = _FakeAPIStatusError(200, {"type": "error", "error": {"type": "overloaded_error"}})
    kind, retry_after, _, _ = lc.classify_failure(exc)
    # 不被 status 200 騙成 unknown／成功；判為可退避重試的 overloaded（純指數、無 retry-after）。
    assert kind == "overloaded"
    assert retry_after is None


def test_classify_failure_sse_body_rate_limit_ignores_status_200():
    exc = _FakeAPIStatusError(200, {"type": "error", "error": {"type": "rate_limit_error"}})
    kind, _, _, _ = lc.classify_failure(exc)
    assert kind == "rate_limit"


# --- classify_failure：例外分類 ----------------------------------------


def test_classify_failure_signal_objects():
    assert lc.classify_failure(lc.RateLimitSignal(3.0, "snip", "partial")) == (
        "rate_limit",
        3.0,
        "snip",
        "partial",
    )
    assert lc.classify_failure(lc.APIErrorSignal("overloaded_error", "snip", "p")) == (
        "api_error",
        None,
        "snip",
        "p",
    )


def test_classify_failure_exception_text_429():
    kind, retry_after, _, _ = lc.classify_failure(RuntimeError(_RATE_LIMIT_JSON))
    assert kind == "rate_limit"


def test_classify_failure_unknown():
    assert lc.classify_failure(ValueError("boom")) == ("unknown", None, "", "")


# --- backoff_delay：retry-after 優先、指數、夾 cap ----------------------


def test_backoff_prefers_retry_after_and_caps():
    assert lc.backoff_delay(5.0, 0, base=2.0, cap=10.0) == 5.0
    assert lc.backoff_delay(99.0, 0, base=2.0, cap=10.0) == 10.0  # retry-after 也夾 cap
    assert lc.backoff_delay(None, 0, base=2.0, cap=10.0) == 2.0  # 指數 2×2^0
    assert lc.backoff_delay(None, 2, base=2.0, cap=10.0) == 8.0  # 2×2^2
    assert lc.backoff_delay(None, 5, base=2.0, cap=10.0) == 10.0  # 夾 cap


def test_backoff_jitter_default_off_is_deterministic():
    # jitter 預設 0：回傳確定值，與舊行為等價（保證既有測試零回歸）。
    assert lc.backoff_delay(5.0, 0, base=2.0, cap=10.0, jitter=0.0) == 5.0
    assert lc.backoff_delay(None, 1, base=2.0, cap=10.0, jitter=0.0) == 4.0


# --- 429 路徑 jitter：以 retry-after 為主、僅向上、夾 cap ----------------


def test_backoff_429_jitter_upper_lower_bounds():
    # 429：nominal=min(retry_after,cap)=4；jitter=0.5 → 落點 ∈ [4, 4×1.5=6]。
    lo = lc.backoff_delay(4.0, 0, base=2.0, cap=60.0, jitter=0.5, rand=lambda: 0.0)
    hi = lc.backoff_delay(4.0, 0, base=2.0, cap=60.0, jitter=0.5, rand=lambda: 1.0)
    assert lo == 4.0  # 永不早於伺服器 retry-after
    assert hi == 6.0  # 上界 nominal×(1+jitter)


def test_backoff_429_jitter_never_below_retry_after_and_capped():
    # 多次隨機抽樣：429 退避恆 ≥ retry-after（夾 cap 後），且不超過 cap。
    import random as _r

    rng = _r.Random(1234)
    for _ in range(200):
        d = lc.backoff_delay(5.0, 0, base=2.0, cap=20.0, jitter=0.4, rand=rng.random)
        assert 5.0 <= d <= min(5.0 * 1.4, 20.0)
    # retry-after 超過 cap：nominal 先夾為 cap，向上 jitter 仍夾回 cap。
    d = lc.backoff_delay(99.0, 0, base=2.0, cap=10.0, jitter=0.5, rand=lambda: 1.0)
    assert d == 10.0


# --- 529／無 retry-after 路徑 jitter：純指數、equal-jitter 向下散開 -------


def test_backoff_529_exponential_jitter_bounds():
    # 529：nominal=min(base×2^attempt, cap)=8；jitter=0.5 → 落點 ∈ [8×0.5=4, 8]。
    full = lc.backoff_delay(None, 2, base=2.0, cap=60.0, jitter=0.5, rand=lambda: 0.0)
    half = lc.backoff_delay(None, 2, base=2.0, cap=60.0, jitter=0.5, rand=lambda: 1.0)
    assert full == 8.0  # rand=0 → 不扣減，等於 nominal（上界）
    assert half == 4.0  # rand=1 → nominal×(1-jitter)（下界）


def test_backoff_529_jitter_within_band_and_capped():
    import random as _r

    rng = _r.Random(99)
    for attempt in range(6):
        nominal = min(2.0 * (2**attempt), 30.0)
        d = lc.backoff_delay(None, attempt, base=2.0, cap=30.0, jitter=0.5, rand=rng.random)
        assert nominal * 0.5 <= d <= nominal  # 落在 equal-jitter 帶內、不超 cap


def test_backoff_jitter_out_of_range_raises():
    # 架構決策：jitter 超界＝呼叫端 bug，報錯而非靜默夾值（統一 cfg／純量兩路徑語意）。
    with pytest.raises(ValueError):
        lc.backoff_delay(4.0, 0, base=2.0, cap=60.0, jitter=5.0, rand=lambda: 1.0)
    with pytest.raises(ValueError):
        lc.backoff_delay(None, 1, base=2.0, cap=60.0, jitter=-0.5, rand=lambda: 1.0)
    # NaN 也被攔下（NaN 任何比較皆 False）。
    with pytest.raises(ValueError):
        lc.backoff_delay(None, 1, base=2.0, cap=60.0, jitter=float("nan"))


# --- backoff_delay：cfg（RetryConfig）入口 ----------------------------


def test_backoff_cfg_equivalent_to_scalars():
    # cfg 路徑與等價純量路徑回傳相同（jitter=0 確定值）。
    cfg = lc.RetryConfig(max_retries=3, base_delay=2.0, cap=10.0, jitter=0.0)
    for ra, att in [(5.0, 0), (99.0, 0), (None, 0), (None, 2), (None, 5)]:
        assert lc.backoff_delay(ra, att, cfg=cfg) == lc.backoff_delay(
            ra, att, base=2.0, cap=10.0, jitter=0.0
        )


def test_backoff_cfg_shadows_scalar_params():
    # 傳 cfg 時 base/cap/jitter 純量完全被屏蔽，一律取 cfg 欄位。
    cfg = lc.RetryConfig(base_delay=2.0, cap=10.0, jitter=0.0)
    # 即使純量給了離譜值，輸出仍由 cfg 決定（529 指數：2×2^2=8）。
    assert lc.backoff_delay(None, 2, base=999.0, cap=1.0, jitter=1.0, cfg=cfg) == 8.0


def test_backoff_cfg_jitter_bounds():
    # cfg.jitter>0：429 向上加、夾 cap，注入固定 rand 驗上界。
    cfg = lc.RetryConfig(base_delay=2.0, cap=60.0, jitter=0.5)
    assert lc.backoff_delay(4.0, 0, cfg=cfg, rand=lambda: 1.0) == 4.0 * 1.5
    # 529 equal-jitter 向下：nominal×(1-0.5×1.0)=8×0.5=4。
    assert lc.backoff_delay(None, 2, cfg=cfg, rand=lambda: 1.0) == 4.0


def test_make_backoff_fn_matches_backoff_delay():
    cfg = lc.RetryConfig(base_delay=2.0, cap=10.0, jitter=0.0)
    fn = cfg.make_backoff_fn()
    assert fn(None, 2) == lc.backoff_delay(None, 2, cfg=cfg)
    assert fn(5.0, 0) == lc.backoff_delay(5.0, 0, cfg=cfg)


# --- run_with_retries：骨幹控制流 --------------------------------------


async def test_run_with_retries_requires_max_retries_or_cfg():
    # max_retries 與 cfg 皆 None＝呼叫端未指定上限，報 ValueError。
    async def attempt():
        return "ok"

    async def on_rl(s, p):
        return "rl"

    async def on_api(s, p):
        return "api"

    with pytest.raises(ValueError):
        await lc.run_with_retries(
            attempt,
            on_rate_limit_exhausted=on_rl,
            on_api_error=on_api,
        )


async def test_run_with_retries_uses_cfg_max_and_backoff():
    # 只給 cfg（不給 max_retries／backoff）：上限與退避皆由 cfg 取得。
    delays: list[float] = []

    async def sleep(s):
        delays.append(s)

    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        raise lc.RateLimitSignal(None, "429")

    async def on_rl(s, p):
        return "exhausted"

    async def on_api(s, p):
        return "api"

    cfg = lc.RetryConfig(max_retries=2, base_delay=2.0, cap=60.0, jitter=0.0)
    out = await lc.run_with_retries(
        attempt,
        cfg=cfg,
        on_rate_limit_exhausted=on_rl,
        on_api_error=on_api,
        sleep=sleep,
    )
    assert out == "exhausted"
    assert calls["n"] == 3  # 初次 + 2 次重試
    assert delays == [2.0, 4.0]  # cfg 退避：2×2^0, 2×2^1


@pytest.fixture
def recorder():
    """共用注入：記錄 sleep 延遲與 before_sleep hook 呼叫。"""
    delays: list[float] = []
    retries: list[tuple] = []

    async def sleep(s):
        delays.append(s)

    async def on_retry(attempt, limit, delay, snippet):
        retries.append((attempt, limit, delay))

    return delays, retries, sleep, on_retry


async def test_run_rate_limit_retry_then_success(recorder):
    delays, retries, sleep, on_retry = recorder
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        if calls["n"] == 1:
            raise lc.RateLimitSignal(None, "limited")
        return "ok"

    async def exhausted(snip, partial):  # 不應被呼叫
        raise AssertionError

    async def api_err(snip, partial):
        raise AssertionError

    out = await lc.run_with_retries(
        attempt,
        max_retries=2,
        on_rate_limit_exhausted=exhausted,
        on_api_error=api_err,
        backoff=lambda ra, a: 2.0 * (2**a),
        sleep=sleep,
        on_retry=on_retry,
    )
    assert out == "ok"
    assert calls["n"] == 2
    assert delays == [2.0]
    assert retries == [(0, 2, 2.0)]  # before_sleep hook 收到 attempt=0、limit=2、delay=2.0


async def test_run_rate_limit_exhausted_fallback(recorder):
    delays, _, sleep, on_retry = recorder

    async def attempt():
        raise lc.RateLimitSignal(None, "always-limited", "半句")

    async def exhausted(snip, partial):
        return f"FALLBACK:{snip}:{partial}"

    async def api_err(snip, partial):
        raise AssertionError

    out = await lc.run_with_retries(
        attempt,
        max_retries=2,
        on_rate_limit_exhausted=exhausted,
        on_api_error=api_err,
        backoff=lambda ra, a: 2.0 * (2**a),
        sleep=sleep,
        on_retry=on_retry,
    )
    assert out == "FALLBACK:always-limited:半句"
    assert delays == [2.0, 4.0]  # RETRIES=2：指數退避兩次


async def test_run_api_error_no_retry():
    delays: list[float] = []

    async def sleep(s):
        delays.append(s)

    async def attempt():
        raise lc.APIErrorSignal("overloaded_error", "over", "半句")

    async def exhausted(snip, partial):
        raise AssertionError

    async def api_err(snip, partial):
        return f"APIERR:{snip}:{partial}"

    out = await lc.run_with_retries(
        attempt,
        max_retries=3,
        on_rate_limit_exhausted=exhausted,
        on_api_error=api_err,
        sleep=sleep,
    )
    assert out == "APIERR:over:半句"
    assert delays == []  # 不重試、不退避


async def test_run_overloaded_529_retries_pure_exponential_then_fallback(recorder):
    # 529 過載：可退避重試，但走純指數退避（忽略任何 retry_after），耗盡後走 on_api_error。
    delays, retries, sleep, on_retry = recorder
    seen_retry_after: list = []

    async def attempt():
        raise lc.OverloadedSignal("overloaded_error", "overloaded", "半句")

    async def exhausted(snip, partial):  # 529 不應走限流 fallback
        raise AssertionError

    async def api_err(snip, partial):
        return f"APIERR:{snip}:{partial}"

    def backoff(retry_after, attempt):
        seen_retry_after.append(retry_after)  # 證明 529 一律收到 retry_after=None
        return 2.0 * (2**attempt)

    out = await lc.run_with_retries(
        attempt,
        max_retries=2,
        on_rate_limit_exhausted=exhausted,
        on_api_error=api_err,
        backoff=backoff,
        sleep=sleep,
        on_retry=on_retry,
    )
    assert out == "APIERR:overloaded:半句"  # 耗盡後走通用 API 錯誤 fallback
    assert delays == [2.0, 4.0]  # 純指數退避兩次（RETRIES=2）
    assert seen_retry_after == [None, None]  # 529 路徑強制 retry_after=None
    assert retries == [(0, 2, 2.0), (1, 2, 4.0)]  # before_sleep hook 收到兩次


async def test_run_overloaded_retry_then_success(recorder):
    # 529 退避後該輪成功：不走 fallback，回傳正常結果。
    delays, _, sleep, on_retry = recorder
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        if calls["n"] == 1:
            raise lc.OverloadedSignal("overloaded_error", "overloaded")
        return "ok"

    async def noop(*a):
        raise AssertionError

    out = await lc.run_with_retries(
        attempt,
        max_retries=3,
        on_rate_limit_exhausted=noop,
        on_api_error=noop,
        backoff=lambda ra, a: 2.0 * (2**a),
        sleep=sleep,
        on_retry=on_retry,
    )
    assert out == "ok"
    assert calls["n"] == 2
    assert delays == [2.0]


async def test_run_passthrough_handled():
    class Timeout(Exception):
        pass

    async def attempt():
        raise Timeout("idle")

    async def passthrough_handler(exc):
        return f"TIMEOUT:{exc}"

    async def noop(*a):
        raise AssertionError

    out = await lc.run_with_retries(
        attempt,
        max_retries=3,
        on_rate_limit_exhausted=noop,
        on_api_error=noop,
        passthrough=(Timeout,),
        on_passthrough=passthrough_handler,
    )
    assert out == "TIMEOUT:idle"


async def test_run_unknown_reraised():
    async def attempt():
        raise ValueError("boom")

    async def noop(*a):
        raise AssertionError

    with pytest.raises(ValueError, match="boom"):
        await lc.run_with_retries(
            attempt,
            max_retries=3,
            on_rate_limit_exhausted=noop,
            on_api_error=noop,
        )
