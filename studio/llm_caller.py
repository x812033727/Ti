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
import re
from collections.abc import Awaitable, Callable

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

# --- SSE 錯誤型別 → 規範 HTTP 狀態碼對照表（Issue #1258）-----------------------
# Anthropic SDK 已知 Bug：串流中途收到 SSE `error` 事件（如 overloaded_error）時，
# SDK 把原始 HTTP 200 response 交給 _make_status_error()，於是拋出 status_code=200，
# 任何依賴 `status_code >= 500` 的 retry 邏輯都「靜默失效」（受害者含 pydantic-ai
# FallbackModel）。對策：自建型別→狀態碼對照表，串流偵測一律以「錯誤型別」判定，
# 不信任 SDK 拋出的 status_code，直到官方修掉 #1258。對照表即「該型別是否可退避重試」
# 的單一事實來源（429→限流可退避；其餘→fallback），供分類器與 SSE 防線共用。
_SSE_ERROR_TYPE_TO_STATUS: dict[str, int] = {
    "rate_limit_error": 429,
    "overloaded_error": 529,
    "api_error": 500,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "invalid_request_error": 400,
    "billing_error": 403,
    "timeout_error": 408,
}
# 視為「限流、可退避重試」的狀態碼（對照表唯一的可重試碼）。
_RATE_LIMIT_STATUS = 429
# 視為 API 錯誤（走 fallback）的狀態碼；其餘僅當有錯誤型別 token 才算。
# 由對照表的非限流碼自動帶入，再補上 SDK 直接吐出的 5xx/4xx 變體（502/503）。
_API_ERROR_CODES = {
    str(code) for code in _SSE_ERROR_TYPE_TO_STATUS.values() if code != _RATE_LIMIT_STATUS
} | {"502", "503"}

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


def sse_error_status(error_type: str | None) -> int | None:
    """SSE error 事件型別 → 規範 HTTP 狀態碼（Issue #1258：不信任 SDK status_code）。

    這是串流防線判型的單一查表入口：`overloaded_error→529`、`rate_limit_error→429`…。
    未知／空型別回傳 None（呼叫端可保守視為 api_error）。
    """
    if not error_type:
        return None
    return _SSE_ERROR_TYPE_TO_STATUS.get(error_type.strip())


def is_rate_limit_type(error_type: str | None) -> bool:
    """該 SSE 錯誤型別是否屬「限流、可退避重試」（依對照表，唯一可重試碼＝429）。"""
    return sse_error_status(error_type) == _RATE_LIMIT_STATUS


def classify_api_text(text: str) -> tuple[str, object] | None:
    """判斷一段文字是否為 API 錯誤封包。

    回傳 ("rate_limit", retry_after|None) ／ ("api_error", kind) ／ None。
    判型一律「錯誤型別優先」：型別 token 經 `_SSE_ERROR_TYPE_TO_STATUS` 查表決定是否限流，
    完全不信任文字裡夾帶的 SDK status_code（Issue #1258：SSE error 常被標成 200/亂碼）。
    只有在「沒有任何錯誤型別 token」時，才退而採用明確前綴後的狀態碼判定。
    """
    if not text:
        return None
    m = _API_ERR_RE.search(text)
    kind = m.group("kind") if m else None
    # 錯誤型別優先：查表決定限流／fallback，忽略 SDK 夾帶的 status_code。
    if kind is not None:
        if is_rate_limit_type(kind):
            return ("rate_limit", parse_retry_after(text))
        return ("api_error", kind)
    # 無型別 token：才回退到狀態碼判定。
    sm = _STATUS_RE.search(text)
    code = sm.group("code") if sm else None
    if code == str(_RATE_LIMIT_STATUS):
        return ("rate_limit", parse_retry_after(text))
    if code and code in _API_ERROR_CODES:
        return ("api_error", f"HTTP {code}")
    return None


def classify_sse_error(
    error_type: str | None, message: str = "", *, partial_text: str = ""
) -> Exception:
    """把串流 SSE `error` 事件依「錯誤型別」歸類成對應訊號例外，完全忽略 SDK 的 status_code。

    Issue #1258 的核心修法：SDK 把 SSE error 包成 status_code=200，依 status code 的 retry
    全部靜默失效；本函式只看 error_type → `_SSE_ERROR_TYPE_TO_STATUS`：
    - 429（rate_limit_error）→ `RateLimitSignal`（可退避重試，順帶解析 message 內的 retry-after）。
    - 其餘已知型別（overloaded_error→529…）→ `APIErrorSignal`（走 fallback、不重試）。
    - 未知／空型別 → 保守視為 `APIErrorSignal`，不誤判為可無限重試的限流。
    回傳 Signal 例外實例（呼叫端 `raise` 即可），不在此處 raise 以利測試與組合。
    """
    snippet = (message or error_type or "sse_error")[:300]
    if is_rate_limit_type(error_type):
        return RateLimitSignal(parse_retry_after(message), snippet, partial_text)
    kind = (error_type or "").strip() or "sse_error"
    return APIErrorSignal(kind, snippet, partial_text)


def _sse_error_type_from_exc(exc: Exception) -> str | None:
    """從 SDK 例外的結構化 body 擷取 SSE 錯誤型別（Issue #1258：status_code 不可信）。

    Anthropic SDK 把 SSE error 包成 `APIStatusError(status_code=200)`，但仍把原始錯誤
    封包掛在 `.body`（形如 `{"type":"error","error":{"type":"overloaded_error",...}}`）。
    這裡只讀 `.body` 的型別欄位，完全不看 `.status_code`，已知型別才回傳。
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        etype = err.get("type") if isinstance(err, dict) else body.get("type")
        if etype in _SSE_ERROR_TYPE_TO_STATUS:
            return etype
    return None


def classify_failure(exc: Exception) -> tuple[str, float | None, str, str]:
    """把 stream/query 拋出的例外歸類為 rate_limit／api_error／unknown。

    回傳 (kind, retry_after, snippet, partial_text)。涵蓋三種 SDK 失敗形態：
    (a) 本層從錯誤文字主動拋出的 RateLimitSignal／APIErrorSignal（含其子類）；
    (b) SSE error 例外（Issue #1258）——SDK 標成 status 200 但 `.body` 帶真實型別，
        以對照表判型、忽略 status_code；
    (c) SDK 例外型 429（issue #812）——以 str(exc) 套同一錨定樣式辨識。
    unknown 不吞，由呼叫端 re-raise，不掩蓋真正的程式錯誤。
    """
    if isinstance(exc, RateLimitSignal):
        return ("rate_limit", exc.retry_after, exc.snippet, exc.partial_text)
    if isinstance(exc, APIErrorSignal):
        return ("api_error", None, exc.snippet, exc.partial_text)
    # (b) 結構化 SSE error body：型別優先、忽略 SDK 的 status_code（200）。
    etype = _sse_error_type_from_exc(exc)
    if etype is not None:
        snippet = str(exc)[:300] or etype
        if is_rate_limit_type(etype):
            return ("rate_limit", parse_retry_after(str(exc)), snippet, "")
        return ("api_error", None, snippet, "")
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
    """
    if backoff is None:
        backoff = backoff_delay
    limit = max(0, max_retries)
    attempt = 0
    while True:
        try:
            return await attempt_fn()
        except passthrough as exc:  # 獨立路徑（如逾時）：不進退避分流
            if on_passthrough is not None:
                return await on_passthrough(exc)
            raise
        except Exception as exc:
            kind, retry_after, snippet, partial = classify_failure(exc)
            if kind == "rate_limit":
                if attempt < limit:
                    delay = backoff(retry_after, attempt)
                    if on_retry is not None:
                        await on_retry(attempt, limit, delay, snippet)
                    await sleep(delay)
                    attempt += 1
                    continue
                return await on_rate_limit_exhausted(snippet, partial)
            if kind == "api_error":
                return await on_api_error(snippet, partial)
            raise
