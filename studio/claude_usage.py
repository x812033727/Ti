"""Claude 訂閱額度查詢 —— 透過 Anthropic 官方 OAuth usage 端點取得 rate limit。

走訂閱（無 API key）時，Claude Code 的剩餘額度可由 ``api.anthropic.com/api/oauth/usage``
取得：帶上 ``~/.claude/.credentials.json`` 裡的 accessToken 即回傳五小時 / 七天等視窗的
使用百分比與重置時間。比 SDK 的 RateLimitEvent 好處是「可隨時主動查」，不必等討論在跑。

純 HTTP/IO、與 LLM 解耦，方便單元測試（monkeypatch httpx.get 與憑證路徑）。結果以
模組級記憶體 TTL 快取，避免前端反覆按「更新額度」狂打上游。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from . import config

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_HEADERS = {"anthropic-beta": "oauth-2025-04-20"}
_TIMEOUT = 8.0
_TTL = 60.0  # 快取秒數：60s 內重複查直接回上次結果，保護上游也加快面板

# (fetched_at, result)；程序生命週期內共用，重啟即清空（無持久化、無狀態檔）。
_cache: tuple[float, dict] | None = None


def _read_token() -> str | None:
    """從 OAuth 憑證檔讀 accessToken；缺檔／壞檔／無 token 皆回 None。"""
    try:
        data = json.loads(config.CLAUDE_CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tok = (data.get("claudeAiOauth") or {}).get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _token_expired(now: float) -> bool:
    """憑證檔的 accessToken 是否已過期（expiresAt 為毫秒 epoch）。讀不到視為未過期。

    access token 約每小時過期、僅在 Claude CLI/SDK 跑時刷新；過期時打 usage 端點必得 401。
    先用 expiresAt 短路，省一次註定失敗的 8s HTTP，並讓前端拿到明確「過期」而非空白。
    """
    try:
        data = json.loads(config.CLAUDE_CREDENTIALS_FILE.read_text(encoding="utf-8"))
        exp = (data.get("claudeAiOauth") or {}).get("expiresAt")
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(exp, (int, float)) and now >= exp / 1000.0


def _iso_to_epoch(s: Any) -> float | None:
    """ISO8601（含時區與小數秒）→ unix 秒；非字串或解析失敗回 None。"""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _window(d: Any) -> dict | None:
    """單一視窗（如 five_hour）正規化為 {used_percentage, reset_at(epoch)}；None→None。"""
    if not isinstance(d, dict):
        return None
    util = d.get("utilization")
    return {
        "used_percentage": round(float(util), 1) if isinstance(util, (int, float)) else None,
        "reset_at": _iso_to_epoch(d.get("resets_at")),
    }


def _empty(error: str, now: float) -> dict:
    return {
        "five_hour": None,
        "seven_day": None,
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "fetched_at": now,
        "error": error,
    }


def fetch_rate_limits(force: bool = False) -> dict:
    """查 Claude 訂閱 rate limit。回傳正規化 dict（含 error 欄位，永不拋例外）。

    error ∈ {None, "token_missing", "unauthorized", "unreachable"}。
    force=True 繞過 TTL 快取（保留給未來「強制刷新」用；目前一律走快取）。
    """
    global _cache
    now = _now()
    if not force and _cache is not None and now - _cache[0] < _TTL:
        return _cache[1]

    token = _read_token()
    if not token:
        result = _empty("token_missing", now)
        _cache = (now, result)
        return result

    if _token_expired(now):
        # 已知過期：直接回 unauthorized，前端顯示「token 已過期，跑一次討論即恢復」。
        result = _empty("unauthorized", now)
        _cache = (now, result)
        return result

    try:
        resp = httpx.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {token}", **_HEADERS},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        result = _empty("unreachable", now)
        _cache = (now, result)
        return result

    if resp.status_code in (401, 403):
        result = _empty("unauthorized", now)
        _cache = (now, result)
        return result
    if resp.status_code >= 400:
        result = _empty("unreachable", now)
        _cache = (now, result)
        return result

    try:
        body = resp.json()
    except ValueError:
        result = _empty("unreachable", now)
        _cache = (now, result)
        return result

    result = {
        "five_hour": _window(body.get("five_hour")),
        "seven_day": _window(body.get("seven_day")),
        "seven_day_sonnet": _window(body.get("seven_day_sonnet")),
        "seven_day_opus": _window(body.get("seven_day_opus")),
        "fetched_at": now,
        "error": None,
    }
    _cache = (now, result)
    return result


def _now() -> float:
    import time

    return time.time()
