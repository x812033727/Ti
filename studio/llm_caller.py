"""Ti 核心 LLM 韌性中介層 — provider 無關，可被 experts／providers 共用。

把原本散在 `experts.py` 的「錯誤文字分類器」「429／指數退避計算」「重試迴圈骨幹」
抽成單一可重用實作，定義穩定公開介面，讓任何 LLM 呼叫端（具名專家、未來的
provider failover 層、orchestrator 串流層）統一走這層，而不是各自再補一份退避／分類碼。

公開介面（穩定契約，下游請只依賴這些名字）：
- 例外訊號：`RateLimitSignal`（限流，可退避重試）／`APIErrorSignal`（其它 API 錯誤，走
  fallback）。串流／query 層偵測到錯誤文字時 raise 對應訊號，由 `run_with_retries` 收斂。
- 分類器：`classify_api_text(text)`（純文字 → 分類）／`classify_failure(exc)`（例外 → 分類）。
- 退避：`backoff_delay(retry_after, attempt, *, base, cap)`，retry-after 優先、否則指數退避，皆夾 cap。
- 重試骨幹：`run_with_retries(...)`，把「query＋串流 → 限流退避重試／API 錯誤 fallback／
  逾時等獨立路徑直通／未知例外 re-raise」的控制流抽成 provider 無關的協程，呼叫端只需
  注入 attempt_fn 與各 callback。

設計原則：本模組不依賴 experts／roles／broadcast／config，只接純量與 callback，確保
provider 無關、易於單元測試（注入 fake sleep 即零實際等待）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Anthropic 錯誤封包形如 {"type":"error","error":{"type":"rate_limit_error",...}}。
# 錨定「JSON key:value 的錯誤型別 token」而非裸關鍵字，避免專家正常引用「rate limit／
# error」字樣被誤殺（架構決策：禁用寬鬆關鍵字）。
_API_ERR_RE = re.compile(
    r'"type"\s*:\s*"(?P<kind>rate_limit_error|overloaded_error|api_error|'
    r"authentication_error|permission_error|not_found_error|request_too_large|"
    r'invalid_request_error|billing_error|timeout_error)"'
)
# 僅在 status/error code/HTTP 等明確前綴後出現的數字才視為狀態碼（裸數字不算）。
_STATUS_RE = re.compile(
    r"(?:status(?:\s*code)?|error\s*code|HTTP)\D{0,8}(?P<code>4\d\d|5\d\d)", re.I
)
# retry-after：header 或 JSON 欄位皆容忍（秒）。
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[\"'\s:=]+(?P<sec>\d+(?:\.\d+)?)", re.I)
# 視為 API 錯誤（走 fallback）的狀態碼；其餘僅當有錯誤型別 token 才算。
_API_ERROR_CODES = {"400", "401", "403", "404", "413", "500", "502", "503", "529"}

# 退避預設值（純量預設，不讀 config——呼叫端自行帶入專案設定，保持 provider 無關）。
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_CAP = 60.0


class RateLimitSignal(Exception):
    """偵測到 429／rate_limit_error——交由 `run_with_retries` 做有限次退避重試。

    snippet＝命中錯誤的原文片段（供 log）；partial_text＝命中前已收到的合法文字。
    retry_after＝從錯誤文字／例外解析到的建議等待秒數（無則 None，改走指數退避）。
    """

    def __init__(self, retry_after: float | None, snippet: str, partial_text: str = ""):
        super().__init__(snippet)
        self.retry_after = retry_after
        self.snippet = snippet
        self.partial_text = partial_text


class APIErrorSignal(Exception):
    """偵測到非限流的 API 錯誤文字（如 overloaded_error）——視為該輪失敗走 fallback。

    與限流分屬兩條獨立失敗路徑：不重試，直接走呼叫端提供的 fallback 收斂。
    """

    def __init__(self, kind: str, snippet: str, partial_text: str = ""):
        super().__init__(snippet)
        self.kind = kind
        self.snippet = snippet
        self.partial_text = partial_text


def parse_retry_after(text: str) -> float | None:
    """從文字（header 或 JSON）擷取 retry-after 秒數，無則 None。"""
    m = _RETRY_AFTER_RE.search(text or "")
    return float(m.group("sec")) if m else None


def classify_api_text(text: str) -> tuple[str, object] | None:
    """判斷一段文字是否為 API 錯誤封包。

    回傳 ("rate_limit", retry_after|None) ／ ("api_error", kind) ／ None。
    rate_limit 條件：型別 token 為 rate_limit_error，或明確前綴後的狀態碼為 429。
    """
    if not text:
        return None
    m = _API_ERR_RE.search(text)
    kind = m.group("kind") if m else None
    sm = _STATUS_RE.search(text)
    code = sm.group("code") if sm else None
    if kind == "rate_limit_error" or code == "429":
        return ("rate_limit", parse_retry_after(text))
    if kind:
        return ("api_error", kind)
    if code and code in _API_ERROR_CODES:
        return ("api_error", f"HTTP {code}")
    return None


def classify_failure(exc: Exception) -> tuple[str, float | None, str, str]:
    """把 stream/query 拋出的例外歸類為 rate_limit／api_error／unknown。

    回傳 (kind, retry_after, snippet, partial_text)。涵蓋兩種 SDK 失敗形態：
    (a) 本層從錯誤文字主動拋出的 RateLimitSignal／APIErrorSignal（含其子類）；
    (b) SDK 例外型 429（issue #812）——以 str(exc) 套同一錨定樣式辨識。
    unknown 不吞，由呼叫端 re-raise，不掩蓋真正的程式錯誤。
    """
    if isinstance(exc, RateLimitSignal):
        return ("rate_limit", exc.retry_after, exc.snippet, exc.partial_text)
    if isinstance(exc, APIErrorSignal):
        return ("api_error", None, exc.snippet, exc.partial_text)
    hit = classify_api_text(str(exc))
    if hit and hit[0] == "rate_limit":
        return ("rate_limit", hit[1], str(exc)[:300], "")
    if hit and hit[0] == "api_error":
        return ("api_error", None, str(exc)[:300], "")
    return ("unknown", None, "", "")


def backoff_delay(
    retry_after: float | None,
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE,
    cap: float = DEFAULT_BACKOFF_CAP,
) -> float:
    """退避秒數：優先採 retry-after，否則指數退避；皆夾在 cap 內。

    base／cap 由呼叫端帶入（如各專案的 config），本層不讀全域設定以保持 provider 無關。
    """
    if retry_after and retry_after > 0:
        return min(retry_after, cap)
    return min(base * (2**attempt), cap)


# ── 可觀測性接點 ────────────────────────────────────────────────────────────
# 事件名稱（穩定契約，metrics/log sink 請依賴這些字串）。
EV_RETRY = "retry"  # before_sleep：即將退避重試
EV_RATE_LIMIT_EXHAUSTED = "rate_limit_exhausted"  # 限流重試耗盡，走 fallback
EV_API_ERROR = "api_error"  # 非限流 API 錯誤，走 fallback
EV_TIMEOUT = "timeout"  # passthrough 逾時（idle／hard）——獨立路徑
EV_SUCCESS = "success"  # 正常完成
EV_UNKNOWN_ERROR = "unknown_error"  # 未知例外，re-raise

# observe sink 簽章：(event_name, fields) → None。同步、純記錄，不得改變控制流。
Observer = Callable[[str, Mapping[str, object]], None]


@dataclass
class RetryMetrics:
    """重試可觀測性累加器：呼叫端傳入一個實例，`run_with_retries` 於生命週期各點更新，
    呼叫端在返回後讀取，用於 emit metrics（retry 次數、累計延遲、終局）。

    與 idle／hard timeout 正交：逾時走 passthrough，記為 `outcome="timeout"`，**不**計入
    `retries`／`total_delay`（退避迴圈與逾時互不吞沒）。`retries`＝實際發生的退避次數；
    `total_delay`＝累計退避秒數；`rate_limit_hits`＝偵測到限流的總次數（含最後耗盡那次）。
    """

    retries: int = 0
    total_delay: float = 0.0
    last_delay: float = 0.0
    rate_limit_hits: int = 0
    outcome: str = ""
    events: list[str] = field(default_factory=list)

    def _record_retry(self, delay: float) -> None:
        self.retries += 1
        self.rate_limit_hits += 1
        self.last_delay = delay
        self.total_delay += delay

    def to_dict(self) -> dict[str, object]:
        return {
            "retries": self.retries,
            "total_delay": round(self.total_delay, 3),
            "last_delay": round(self.last_delay, 3),
            "rate_limit_hits": self.rate_limit_hits,
            "outcome": self.outcome,
        }


def _emit(observe: Observer | None, event: str, fields: Mapping[str, object]) -> None:
    """安全 emit 一筆可觀測事件：observe sink 拋例外時吞掉並記 log，**絕不**讓觀測性
    破壞重試控制流（向後相容鐵則：加裝觀測接點不得改變既有行為）。"""
    if observe is None:
        return
    try:
        observe(event, fields)
    except Exception:  # noqa: BLE001 — 觀測失敗不可影響主流程
        logger.debug("llm_caller observe sink 拋例外（已忽略）：event=%s", event, exc_info=True)


async def _default_sleep(seconds: float) -> None:
    """run_with_retries 的預設等待實作（非 noop——seconds>0 時真的等）。

    呼叫端通常會注入自己的 sleep（測試 monkeypatch 即零實際等待並記錄延遲）。
    """
    if seconds > 0:
        await asyncio.sleep(seconds)


async def run_with_retries(
    attempt_fn: Callable[[], Awaitable],
    *,
    max_retries: int,
    on_rate_limit_exhausted: Callable[[str, str], Awaitable],
    on_api_error: Callable[[str, str], Awaitable],
    backoff: Callable[[float | None, int], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] = _default_sleep,
    on_retry: Callable[[int, int, float, str], Awaitable[None]] | None = None,
    passthrough: tuple[type[BaseException], ...] = (),
    on_passthrough: Callable[[BaseException], Awaitable] | None = None,
    metrics: RetryMetrics | None = None,
    observe: Observer | None = None,
):
    """provider 無關的重試迴圈骨幹。

    反覆呼叫 attempt_fn（通常＝一次 query()＋串流），依 classify_failure 的分類分流：
    - rate_limit：在 max_retries 內退避重試（延遲由 backoff 計算，預設 backoff_delay），
      重試耗盡呼叫 on_rate_limit_exhausted(snippet, partial) 收斂並回傳其結果。
    - api_error：不重試，直接呼叫 on_api_error(snippet, partial) 收斂並回傳其結果。
    - passthrough（如逾時例外）：屬另一條獨立失敗路徑，不被退避吞掉；有 on_passthrough
      則交它處理並回傳，否則原樣 re-raise。
    - unknown：原樣 re-raise，不掩蓋真正的程式錯誤。

    所有等待都經由注入的 sleep（測試 monkeypatch 即零實際等待並記錄延遲），on_retry 為
    before_sleep 可觀測 hook（attempt 從 0 起、max_retries、本次 delay、snippet）。

    可觀測性（task #4）：
    - `metrics`：傳入一個 `RetryMetrics`，本函式在退避時累加 retries／total_delay，並於
      返回前寫入 `outcome`，供呼叫端 emit；不傳則零成本。
    - `observe(event, fields)`：同步 metrics/log sink，於 before_sleep（EV_RETRY）、限流耗盡
      （EV_RATE_LIMIT_EXHAUSTED）、API 錯誤（EV_API_ERROR）、逾時（EV_TIMEOUT）、成功
      （EV_SUCCESS）、未知例外（EV_UNKNOWN_ERROR）各點觸發。sink 拋例外會被吞掉（`_emit`），
      不影響重試控制流。
    - **正交不互吞**：逾時（passthrough）走 EV_TIMEOUT 並標記 `outcome="timeout"`，**不**經過
      退避分流、**不**計入 retries／total_delay；退避迴圈只處理限流，兩條路徑互不掩蓋。
    """
    if backoff is None:
        backoff = backoff_delay
    if metrics is None:
        metrics = RetryMetrics()
    limit = max(0, max_retries)
    attempt = 0
    while True:
        try:
            result = await attempt_fn()
        except passthrough as exc:  # 獨立路徑（如逾時）：不進退避分流、不計入退避 metrics
            metrics.outcome = "timeout"
            _emit(
                observe,
                EV_TIMEOUT,
                {"exc_type": type(exc).__name__, "retries": metrics.retries},
            )
            if on_passthrough is not None:
                return await on_passthrough(exc)
            raise
        except Exception as exc:
            kind, retry_after, snippet, partial = classify_failure(exc)
            if kind == "rate_limit":
                if attempt < limit:
                    delay = backoff(retry_after, attempt)
                    metrics._record_retry(delay)
                    _emit(
                        observe,
                        EV_RETRY,
                        {
                            "attempt": attempt,
                            "max_retries": limit,
                            "delay": delay,
                            "retry_after": retry_after,
                            "total_delay": metrics.total_delay,
                            "snippet": snippet,
                        },
                    )
                    if on_retry is not None:
                        await on_retry(attempt, limit, delay, snippet)
                    await sleep(delay)
                    attempt += 1
                    continue
                metrics.rate_limit_hits += 1
                metrics.outcome = "rate_limit_exhausted"
                _emit(
                    observe,
                    EV_RATE_LIMIT_EXHAUSTED,
                    {"retries": metrics.retries, "total_delay": metrics.total_delay, "snippet": snippet},
                )
                return await on_rate_limit_exhausted(snippet, partial)
            if kind == "api_error":
                metrics.outcome = "api_error"
                _emit(observe, EV_API_ERROR, {"snippet": snippet, "retries": metrics.retries})
                return await on_api_error(snippet, partial)
            metrics.outcome = "unknown_error"
            _emit(observe, EV_UNKNOWN_ERROR, {"exc_type": type(exc).__name__})
            raise
        else:
            metrics.outcome = "success"
            _emit(
                observe,
                EV_SUCCESS,
                {"retries": metrics.retries, "total_delay": metrics.total_delay},
            )
            return result
