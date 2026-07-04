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
import random
import re
import warnings
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
# 兩個「可退避重試」的狀態碼：429（限流，retry-after 優先）／529（過載，純指數退避）。
_RATE_LIMIT_STATUS = 429
_OVERLOADED_STATUS = 529
# 視為 API 錯誤（走 fallback、不重試）的狀態碼；其餘僅當有錯誤型別 token 才算。
# 由對照表的「非可退避」碼自動帶入（排除 429／529），再補 SDK 直接吐出的 5xx/4xx 變體（502/503）。
_API_ERROR_CODES = {
    str(code)
    for code in _SSE_ERROR_TYPE_TO_STATUS.values()
    if code not in (_RATE_LIMIT_STATUS, _OVERLOADED_STATUS)
} | {"502", "503"}

# 退避預設值（純量預設，不讀 config——呼叫端自行帶入專案設定，保持 provider 無關）。
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_CAP = 60.0
# jitter 預設關閉（0＝回傳確定值，與既有行為等價）；呼叫端按需開啟以防 thundering herd。
DEFAULT_BACKOFF_JITTER = 0.0


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


class OverloadedSignal(Exception):
    """偵測到 529／overloaded_error——伺服器過載，可退避重試但**走純指數退避**。

    與 429 分屬兩條退避路徑：529 無 `retry-after`，故 `run_with_retries` 一律以
    retry_after=None 走指數退避＋jitter；重試耗盡後收斂到 on_api_error（不另開 callback，
    保持公開介面穩定）。snippet＝命中片段、partial_text＝命中前已收到的合法文字。
    """

    def __init__(self, kind: str, snippet: str, partial_text: str = ""):
        super().__init__(snippet)
        self.kind = kind
        self.snippet = snippet
        self.partial_text = partial_text


class APIErrorSignal(Exception):
    """偵測到非限流／非過載的 API 錯誤文字（如 auth/HTTP 503）——視為該輪失敗走 fallback。

    與限流（429 退避）／過載（529 退避）分屬獨立失敗路徑：不重試，直接走呼叫端 fallback 收斂。
    """

    def __init__(self, kind: str, snippet: str, partial_text: str = ""):
        super().__init__(snippet)
        self.kind = kind
        self.snippet = snippet
        self.partial_text = partial_text


class ProviderUnavailable(RuntimeError):
    """上游 provider 暫時不可用；應停止本場而非把錯誤文字交給 QA 判決。"""

    def __init__(self, provider: str, detail: str):
        super().__init__(detail)
        self.provider = provider
        self.detail = detail


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
    """該 SSE 錯誤型別是否屬「限流（429），可退避重試（retry-after 優先）」。"""
    return sse_error_status(error_type) == _RATE_LIMIT_STATUS


def is_overloaded_type(error_type: str | None) -> bool:
    """該 SSE 錯誤型別是否屬「過載（529），可退避重試（純指數退避、無 retry-after）」。"""
    return sse_error_status(error_type) == _OVERLOADED_STATUS


def classify_api_text(text: str) -> tuple[str, object] | None:
    """判斷一段文字是否為 API 錯誤封包。

    回傳 ("rate_limit", retry_after|None) ／ ("overloaded", kind) ／ ("api_error", kind) ／ None。
    判型一律「錯誤型別優先」：型別 token 經 `_SSE_ERROR_TYPE_TO_STATUS` 查表決定分流，
    完全不信任文字裡夾帶的 SDK status_code（Issue #1258：SSE error 常被標成 200/亂碼）。
    分流（依序）：
    - rate_limit（429 退避，retry-after 優先）：型別 token 查表為 429，或無型別時前綴狀態碼為 429。
    - overloaded（529 退避，純指數）：型別 token 查表為 529（如 overloaded_error），或無型別時狀態碼為 529。
    - api_error（不重試走 fallback）：其餘已知錯誤型別 token，或 _API_ERROR_CODES 內的狀態碼。
    只有在「沒有任何錯誤型別 token」時，才退而採用明確前綴後的狀態碼判定。
    """
    if not text:
        return None
    m = _API_ERR_RE.search(text)
    kind = m.group("kind") if m else None
    # 錯誤型別優先：查表決定限流／過載／fallback，忽略 SDK 夾帶的 status_code。
    if kind is not None:
        if is_rate_limit_type(kind):
            return ("rate_limit", parse_retry_after(text))
        if is_overloaded_type(kind):
            return ("overloaded", kind)
        return ("api_error", kind)
    # 無型別 token：才回退到狀態碼判定。
    sm = _STATUS_RE.search(text)
    code = sm.group("code") if sm else None
    if code == str(_RATE_LIMIT_STATUS):
        return ("rate_limit", parse_retry_after(text))
    if code == str(_OVERLOADED_STATUS):
        return ("overloaded", f"HTTP {_OVERLOADED_STATUS}")
    if code and code in _API_ERROR_CODES:
        return ("api_error", f"HTTP {code}")
    return None


_PROVIDER_UNAVAILABLE_API_KINDS: dict[str, tuple[str, str]] = {
    "billing_error": ("billing", "billing or quota is unavailable"),
    "authentication_error": ("auth", "provider authentication failed"),
    "permission_error": ("permission", "provider permission denied"),
    "timeout_error": ("timeout", "provider request timed out"),
}
_PROVIDER_UNAVAILABLE_HTTP_KINDS: dict[str, tuple[str, str]] = {
    "HTTP 401": ("auth", "provider authentication failed"),
    "HTTP 403": ("permission", "provider permission denied"),
    "HTTP 408": ("timeout", "provider request timed out"),
    "HTTP 502": ("server", "provider gateway error"),
    "HTTP 503": ("server", "provider service unavailable"),
}
_PROVIDER_UNAVAILABLE_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, str]], ...] = (
    (
        re.compile(
            r"\byou(?:'ve| have)?\s+(?:hit|reached|exceeded)\s+"
            r"(?:your\s+)?(?:daily\s+)?usage\s+limit\b",
            re.I,
        ),
        ("usage_limit", "usage limit reached"),
    ),
    (
        re.compile(r"\busage\s+limit\s+(?:reached|exceeded|exhausted)\b", re.I),
        ("usage_limit", "usage limit reached"),
    ),
    (
        re.compile(
            r"\b(?:insufficient[_ -]?quota|quota[_ -]?exceeded|resource_exhausted)\b",
            re.I,
        ),
        ("quota", "quota exhausted"),
    ),
    (
        re.compile(
            r"\b(?:quota|billing\s+quota)\b.{0,80}\b(?:exceeded|exhausted|depleted|"
            r"insufficient|reached)\b",
            re.I,
        ),
        ("quota", "quota exhausted"),
    ),
    (
        re.compile(
            r"\b(?:exceeded|exhausted|depleted|insufficient|reached)\b.{0,80}"
            r"\b(?:quota|billing\s+quota)\b",
            re.I,
        ),
        ("quota", "quota exhausted"),
    ),
    (
        re.compile(
            r"\b(?:purchase\s+more\s+credits|credit\s+balance|credits?)\b.{0,80}"
            r"\b(?:exceeded|exhausted|depleted|insufficient|reached|limit)\b",
            re.I,
        ),
        ("quota", "credits exhausted"),
    ),
    (
        re.compile(r"\bbilling_error\b|\bbilling\b.{0,80}\b(?:required|quota|limit)\b", re.I),
        ("billing", "billing or quota is unavailable"),
    ),
    (
        re.compile(
            r"\b(?:authentication\s+required|please\s+sign\s+in|not\s+signed\s+in|"
            r"waiting\s+for\s+authentication|authorization\s+code)\b",
            re.I,
        ),
        ("auth", "provider authentication required"),
    ),
    (
        # 裸 CLI 限流文字（無 HTTP 前綴/JSON error token 的 Codex/Antigravity 輸出）：
        # rate limit 必須與動詞（exceeded/reached/hit）相鄰（雙向），防專家正常
        # 討論 rate limit 設計時被誤殺——Antigravity 的 detail 含 stdout 成功輸出。
        re.compile(
            r"\brate[\s_-]?limits?\s+(?:exceeded|reached|hit)\b"
            r"|\b(?:exceeded|hit|reached)\s+(?:the\s+|your\s+|a\s+)?rate[\s_-]?limits?\b",
            re.I,
        ),
        ("rate_limit", "rate limit reached"),
    ),
    (
        # HTTP 429 慣用語：獨立成行（裸 CLI 典型輸出），或 80 字內帶 429/error/http/retry 錨。
        re.compile(
            r"(?m)^\s*too\s+many\s+requests\.?\s*$"
            r"|\b(?:429|error|http)\b.{0,80}\btoo\s+many\s+requests\b"
            r"|\btoo\s+many\s+requests\b.{0,80}\b(?:429|retry)\b",
            re.I,
        ),
        ("rate_limit", "rate limit reached"),
    ),
    (
        re.compile(r"\b(?:chat|request|provider)\s+timeout\b|\btimed\s+out\b", re.I),
        ("timeout", "provider request timed out"),
    ),
)


def provider_unavailable_kind(text: str) -> tuple[str, str] | None:
    """辨識「不該讓任務繼續重跑」的 provider 不可用文字。

    回傳 (kind, reason)，kind 為 usage_limit/quota/billing/auth/permission/timeout/server/
    rate_limit/overloaded。此函式給 provider 收斂層使用；`classify_api_text` 仍維持嚴格錨定，
    避免正常專家發言提到 quota/rate limit 時被誤殺。
    """
    if not text:
        return None
    hit = classify_api_text(text)
    if hit is not None:
        route, detail = hit
        if route == "rate_limit":
            return ("rate_limit", "rate limit reached")
        if route == "overloaded":
            return ("overloaded", "provider overloaded")
        if route == "api_error":
            value = str(detail)
            if value in _PROVIDER_UNAVAILABLE_API_KINDS:
                return _PROVIDER_UNAVAILABLE_API_KINDS[value]
            if value in _PROVIDER_UNAVAILABLE_HTTP_KINDS:
                return _PROVIDER_UNAVAILABLE_HTTP_KINDS[value]
    for pattern, result in _PROVIDER_UNAVAILABLE_PATTERNS:
        if pattern.search(text):
            return result
    return None


def provider_unavailable_reason(text: str) -> str | None:
    """回傳 provider 不可用的人類可讀原因；無法判定則 None。"""
    hit = provider_unavailable_kind(text)
    return hit[1] if hit is not None else None


def classify_sse_error(
    error_type: str | None, message: str = "", *, partial_text: str = ""
) -> Exception:
    """把串流 SSE `error` 事件依「錯誤型別」歸類成對應訊號例外，完全忽略 SDK 的 status_code。

    Issue #1258 的核心修法：SDK 把 SSE error 包成 status_code=200，依 status code 的 retry
    全部靜默失效；本函式只看 error_type → `_SSE_ERROR_TYPE_TO_STATUS`：
    - 429（rate_limit_error）→ `RateLimitSignal`（可退避重試，順帶解析 message 內的 retry-after）。
    - 529（overloaded_error）→ `OverloadedSignal`（可退避重試，但走純指數退避、無 retry-after）。
    - 其餘已知型別 → `APIErrorSignal`（走 fallback、不重試）。
    - 未知／空型別 → 保守視為 `APIErrorSignal`，不誤判為可無限重試的限流。
    回傳 Signal 例外實例（呼叫端 `raise` 即可），不在此處 raise 以利測試與組合。
    """
    snippet = (message or error_type or "sse_error")[:300]
    if is_rate_limit_type(error_type):
        return RateLimitSignal(parse_retry_after(message), snippet, partial_text)
    kind = (error_type or "").strip() or "sse_error"
    if is_overloaded_type(error_type):
        return OverloadedSignal(kind, snippet, partial_text)
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
    """把 stream/query 拋出的例外歸類為 rate_limit／overloaded／api_error／unknown。

    回傳 (kind, retry_after, snippet, partial_text)。涵蓋三種 SDK 失敗形態：
    (a) 本層主動拋出的 RateLimitSignal／OverloadedSignal／APIErrorSignal（含其子類）；
    (b) SSE error 例外（Issue #1258）——SDK 標成 status 200 但 `.body` 帶真實型別，
        以對照表判型、忽略 status_code；
    (c) SDK 例外型 429／529（issue #812／#1258）——以 str(exc) 套同一錨定樣式辨識。
    rate_limit（429）與 overloaded（529）皆可退避重試，但走不同退避策略（見 run_with_retries）；
    overloaded 無 retry_after（恆 None）。unknown 不吞，由呼叫端 re-raise，不掩蓋真正的程式錯誤。
    """
    if isinstance(exc, RateLimitSignal):
        return ("rate_limit", exc.retry_after, exc.snippet, exc.partial_text)
    if isinstance(exc, OverloadedSignal):
        return ("overloaded", None, exc.snippet, exc.partial_text)
    if isinstance(exc, APIErrorSignal):
        return ("api_error", None, exc.snippet, exc.partial_text)
    # (b) 結構化 SSE error body：型別優先、忽略 SDK 的 status_code（200）。
    etype = _sse_error_type_from_exc(exc)
    if etype is not None:
        snippet = str(exc)[:300] or etype
        if is_rate_limit_type(etype):
            return ("rate_limit", parse_retry_after(str(exc)), snippet, "")
        if is_overloaded_type(etype):
            return ("overloaded", None, snippet, "")
        return ("api_error", None, snippet, "")
    hit = classify_api_text(str(exc))
    if hit and hit[0] == "rate_limit":
        return ("rate_limit", hit[1], str(exc)[:300], "")
    if hit and hit[0] == "overloaded":
        return ("overloaded", None, str(exc)[:300], "")
    if hit and hit[0] == "api_error":
        return ("api_error", None, str(exc)[:300], "")
    if provider_unavailable_kind(str(exc)) is not None:
        return ("api_error", None, str(exc)[:300], "")
    return ("unknown", None, "", "")


def backoff_delay(
    retry_after: float | None,
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE,
    cap: float = DEFAULT_BACKOFF_CAP,
    jitter: float = DEFAULT_BACKOFF_JITTER,
    rand: Callable[[], float] | None = None,
) -> float:
    """退避秒數：429／529 分流退避，皆夾在 cap 內，並可選 jitter 防 thundering herd。

    分流的單一判準＝是否帶 `retry-after`（429 必有伺服器建議值、529 Overloaded 無）：

    - **429 路徑**（retry_after 為正）：以伺服器 `retry-after` 為主，先夾 cap 得 nominal；
      jitter 只「向上」加（最多 ＋jitter×nominal，再夾 cap），確保不早於伺服器要求即重試。
      落點 ∈ [min(retry_after, cap), min(min(retry_after, cap)×(1＋jitter), cap)]。
    - **529／無 retry-after 路徑**：純指數退避 nominal＝min(base×2**attempt, cap)；jitter 採
      equal-jitter「向下」散開，落點 ∈ [nominal×(1－jitter), nominal]，把同時撞限流／過載的
      多端錯開，避免同步重試形成 thundering herd。

    jitter ∈ [0,1]（0＝關閉，回傳確定值，與舊行為等價，故既有測試零回歸）；超出範圍自動夾。
    rand 為隨機源注入縫（預設 `random.random`，回傳 [0,1)），測試可注入固定值驗證退避上下界。
    base／cap 由呼叫端帶入（如各專案的 config），本層不讀全域設定以保持 provider 無關。
    """
    _rand = random.random if rand is None else rand
    j = 0.0 if jitter <= 0 else min(1.0, jitter)
    if retry_after and retry_after > 0:
        # 429：retry-after 為主，jitter 僅向上、夾 cap。
        nominal = min(retry_after, cap)
        if j == 0.0:
            return nominal
        return min(nominal * (1.0 + j * _rand()), cap)
    # 529／無 retry-after：純指數退避，equal-jitter 向下散開。
    nominal = min(base * (2**attempt), cap)
    if j == 0.0:
        return nominal
    return nominal * (1.0 - j * _rand())


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


@dataclass
class RetryConfig:
    """`run_with_retries` 退避三參數的結構化載體（provider 無關，零 config 依賴）。

    對應 `run_with_retries` 的同名 keyword 參數：
    - `max_retries`：限流／過載的最大退避重試次數（`__post_init__` 已 clamp ≥0）。
    - `base`／`cap`／`jitter`：退避退避三旋鈕，當 `backoff is None` 時由 `__post_init__`
      依此自動生成退避 callback（語意同 `backoff_delay(..., base, cap, jitter)`）。
    - `backoff`：退避秒數計算 callback，簽章 `(retry_after, attempt) -> float`；顯式傳入
      時優先於自動生成（`__post_init__` 不覆蓋）。
    - `sleep`：等待實作，簽章 `(seconds) -> Awaitable[None]`（測試可注入零等待）。

    供消費層（如 experts.make_retry_config）以單一物件集中描述 config 驅動的退避策略，
    呼叫端只傳一個物件、再經 `as_kwargs()` 平鋪傳入，取代散傳三個關鍵字參數。

    向後相容：不傳 `base/cap/jitter` 時採模組級 `DEFAULT_BACKOFF_*` 預設（jitter 預設 0
    ＝確定值），自動生成的退避行為與既有 `backoff_delay` 預設等價，既有測試零回歸。

    不可變性警語：`base/cap/jitter` 於建構後視為不可變——建構後更改這些屬性**不會**影響
    `__post_init__` 已生成的 `backoff` callback（閉包在 clamp 完成後固化本地值，不捕捉 self）。
    """

    max_retries: int
    base: float = DEFAULT_BACKOFF_BASE
    cap: float = DEFAULT_BACKOFF_CAP
    jitter: float = DEFAULT_BACKOFF_JITTER
    backoff: Callable[[float | None, int], float] | None = None
    sleep: Callable[[float], Awaitable[None]] = _default_sleep

    def __post_init__(self) -> None:
        # 非法輸入：先 warn 留跡（stacklevel=2 指回呼叫端），再 silent clamp，不拋例外。
        if self.cap <= 0:
            warnings.warn(
                f"RetryConfig.cap={self.cap!r} 非正，已 clamp 為 {DEFAULT_BACKOFF_CAP}",
                stacklevel=2,
            )
            self.cap = DEFAULT_BACKOFF_CAP
        if self.base <= 0:
            warnings.warn(
                f"RetryConfig.base={self.base!r} 非正，已 clamp 為 {DEFAULT_BACKOFF_BASE}",
                stacklevel=2,
            )
            self.base = DEFAULT_BACKOFF_BASE
        if self.max_retries < 0:
            warnings.warn(
                f"RetryConfig.max_retries={self.max_retries!r} 為負，已 clamp 為 0",
                stacklevel=2,
            )
            self.max_retries = 0
        if not (0.0 <= self.jitter <= 1.0):
            warnings.warn(
                f"RetryConfig.jitter={self.jitter!r} 超出 [0,1]，已夾回範圍",
                stacklevel=2,
            )
            self.jitter = max(0.0, min(1.0, self.jitter))
        # 自動生成退避：僅在未顯式注入時觸發；顯式 backoff 優先（設計契約）。
        # 捕捉 clamp 後的本地值（非 self），故建構後更改屬性不影響已生成 callback。
        if self.backoff is None:
            _b, _c, _j = self.base, self.cap, self.jitter
            self.backoff = lambda ra, att: backoff_delay(ra, att, base=_b, cap=_c, jitter=_j)

    def as_kwargs(self) -> dict[str, object]:
        """展開為 `run_with_retries(**cfg.as_kwargs())` 可直接吃的關鍵字字典。

        僅封裝 config 驅動的三參數（max_retries／backoff／sleep）；
        其餘 call-site 專屬 callback（on_rate_limit_exhausted／on_api_error／
        on_retry／passthrough…）仍由呼叫端平鋪傳入，不在此封裝。
        """
        return {
            "max_retries": self.max_retries,
            "backoff": self.backoff,
            "sleep": self.sleep,
        }


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
    - rate_limit（429）：在 max_retries 內退避重試，延遲＝backoff(retry_after, attempt)（以
      retry-after 為主），重試耗盡呼叫 on_rate_limit_exhausted(snippet, partial) 收斂。
    - overloaded（529）：在 max_retries 內退避重試，但延遲＝backoff(None, attempt)（純指數退避，
      強制忽略 retry_after），重試耗盡收斂到 on_api_error(snippet, partial)（共用 callback、
      不另開介面）。429／529 共用同一 max_retries／jitter／cap，僅退避來源不同。
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
            if kind in ("rate_limit", "overloaded"):
                if attempt < limit:
                    # 429 以 retry-after 為主；529 過載無 retry-after，強制走純指數退避。
                    ra = retry_after if kind == "rate_limit" else None
                    delay = backoff(ra, attempt)
                    metrics._record_retry(delay)
                    _emit(
                        observe,
                        EV_RETRY,
                        {
                            "attempt": attempt,
                            "max_retries": limit,
                            "delay": delay,
                            "kind": kind,
                            "retry_after": ra,
                            "total_delay": metrics.total_delay,
                            "snippet": snippet,
                        },
                    )
                    if on_retry is not None:
                        await on_retry(attempt, limit, delay, snippet)
                    await sleep(delay)
                    attempt += 1
                    continue
                # 退避耗盡：429 走限流 fallback；529 過載走通用 API 錯誤 fallback（不另開 callback）。
                if kind == "rate_limit":
                    metrics.rate_limit_hits += 1
                    metrics.outcome = "rate_limit_exhausted"
                    _emit(
                        observe,
                        EV_RATE_LIMIT_EXHAUSTED,
                        {
                            "retries": metrics.retries,
                            "total_delay": metrics.total_delay,
                            "snippet": snippet,
                        },
                    )
                    return await on_rate_limit_exhausted(snippet, partial)
                metrics.outcome = "overloaded_exhausted"
                _emit(
                    observe,
                    EV_API_ERROR,
                    {
                        "snippet": snippet,
                        "retries": metrics.retries,
                        "total_delay": metrics.total_delay,
                        "exhausted": True,
                    },
                )
                return await on_api_error(snippet, partial)
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
