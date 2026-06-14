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
import random
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
# 視為 API 錯誤（走 fallback）的狀態碼；其餘僅當有錯誤型別 token 才算。
_API_ERROR_CODES = {"400", "401", "403", "404", "413", "500", "502", "503", "529"}

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
