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
from pathlib import Path
from typing import Any

import httpx

from . import config

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_HEADERS = {"anthropic-beta": "oauth-2025-04-20"}
_TIMEOUT = 8.0
_TTL = 60.0  # 快取秒數：60s 內重複查直接回上次結果，保護上游也加快面板

# {憑證檔路徑字串: (fetched_at, result)}；per-path，讓多帳號（acct-A/acct-B）各自獨立
# 快取互不污染。程序生命週期內共用，重啟即清空（無持久化、無狀態檔）。
_cache: dict[str, tuple[float, dict]] = {}


def _cred_path(cred_file: Path | None) -> Path:
    """None＝沿用全域線上憑證（config.CLAUDE_CREDENTIALS_FILE），否則用指定檔。"""
    return cred_file if cred_file is not None else config.CLAUDE_CREDENTIALS_FILE


def _read_token(cred_file: Path | None = None) -> str | None:
    """從 OAuth 憑證檔讀 accessToken；缺檔／壞檔／無 token 皆回 None。"""
    try:
        data = json.loads(_cred_path(cred_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tok = (data.get("claudeAiOauth") or {}).get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _token_expired(now: float, cred_file: Path | None = None) -> bool:
    """憑證檔的 accessToken 是否已過期（expiresAt 為毫秒 epoch）。讀不到視為未過期。

    access token 約每小時過期、僅在 Claude CLI/SDK 跑時刷新；過期時打 usage 端點必得 401。
    先用 expiresAt 短路，省一次註定失敗的 8s HTTP，並讓前端拿到明確「過期」而非空白。
    """
    try:
        data = json.loads(_cred_path(cred_file).read_text(encoding="utf-8"))
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


def fetch_rate_limits(force: bool = False, cred_file: Path | None = None) -> dict:
    """查 Claude 訂閱 rate limit。回傳正規化 dict（含 error 欄位，永不拋例外）。

    error ∈ {None, "token_missing", "unauthorized", "unreachable"}。
    force=True 繞過 TTL 快取（保留給未來「強制刷新」用；目前一律走快取）。
    cred_file=None 查全域線上憑證；指定路徑可查特定帳號（acct-A/acct-B）的標籤檔，
    用於設定頁同時顯示多帳號額度。快取以憑證路徑為 key，各帳號互不干擾。
    """
    now = _now()
    path = _cred_path(cred_file)
    key = str(path)
    cached = _cache.get(key)
    if not force and cached is not None and now - cached[0] < _TTL:
        return cached[1]

    def _store(result: dict) -> dict:
        _cache[key] = (now, result)
        return result

    token = _read_token(path)
    if not token:
        return _store(_empty("token_missing", now))

    if _token_expired(now, path):
        # 已知過期：直接回 unauthorized，前端顯示「token 已過期，跑一次討論即恢復」。
        return _store(_empty("unauthorized", now))

    try:
        resp = httpx.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {token}", **_HEADERS},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        return _store(_empty("unreachable", now))

    if resp.status_code in (401, 403):
        return _store(_empty("unauthorized", now))
    if resp.status_code >= 400:
        return _store(_empty("unreachable", now))

    try:
        body = resp.json()
    except ValueError:
        return _store(_empty("unreachable", now))

    return _store(
        {
            "five_hour": _window(body.get("five_hour")),
            "seven_day": _window(body.get("seven_day")),
            "seven_day_sonnet": _window(body.get("seven_day_sonnet")),
            "seven_day_opus": _window(body.get("seven_day_opus")),
            "fetched_at": now,
            "error": None,
        }
    )


def _now() -> float:
    import time

    return time.time()
