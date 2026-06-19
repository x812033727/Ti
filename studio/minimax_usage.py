"""MiniMax 訂閱額度查詢 —— 透過官方 token_plan/remains 端點取得剩餘配額。

MiniMax Token Plan 有官方端點 ``{base}/token_plan/remains``（Bearer = MINIMAX_API_KEY），
回每個 model_name 的「interval（5 小時窗）」與「weekly（7 天窗）」剩餘百分比與窗口時間。
Ti 走文字模型（對應 model_name="general"），故取該筆正規化成與 claude/codex 相同的
five_hour / seven_day 形狀（used_percentage + reset_at），前端 rateLimitBlock 直接重用。

純 HTTP/IO、60s 記憶體 TTL 快取、永不拋例外。
"""

from __future__ import annotations

from typing import Any

import httpx

from . import config

_TIMEOUT = 8.0
_TTL = 60.0
_PREFERRED_MODEL = "general"  # 文字 LLM 類別；找不到時取第一筆

# (fetched_at, result)；程序生命週期內共用，重啟即清空。
_cache: tuple[float, dict] | None = None


def _url() -> str:
    return config.MINIMAX_BASE_URL.rstrip("/") + "/token_plan/remains"


def _ms_to_epoch(v: Any) -> float | None:
    """毫秒 epoch → 秒；非數字回 None。"""
    return float(v) / 1000.0 if isinstance(v, (int, float)) else None


def _used(remaining_percent: Any) -> float | None:
    """remaining_percent(0-100) → used_percentage(0-100)；非數字回 None。"""
    return (
        round(100 - float(remaining_percent), 1)
        if isinstance(remaining_percent, (int, float))
        else None
    )


def _pick_model(model_remains: list) -> dict | None:
    """取 model_name=='general' 的那筆；沒有就取第一筆 dict。"""
    entries = [m for m in model_remains if isinstance(m, dict)]
    for m in entries:
        if m.get("model_name") == _PREFERRED_MODEL:
            return m
    return entries[0] if entries else None


def _empty(error: str, now: float) -> dict:
    return {"five_hour": None, "seven_day": None, "fetched_at": now, "error": error}


def fetch_rate_limits(force: bool = False) -> dict:
    """查 MiniMax 訂閱剩餘額度。回傳正規化 dict（含 error，永不拋例外）。

    error ∈ {None, "token_missing", "unauthorized", "unreachable"}。force=True 繞過快取。
    """
    global _cache
    now = _now()
    if not force and _cache is not None and now - _cache[0] < _TTL:
        return _cache[1]

    key = config.MINIMAX_API_KEY
    if not key:
        result = _empty("token_missing", now)
        _cache = (now, result)
        return result

    try:
        resp = httpx.get(
            _url(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
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

    # base_resp.status_code != 0 代表業務層錯誤（1004=金鑰無效→unauthorized；其餘→unreachable）
    status = ((body or {}).get("base_resp") or {}).get("status_code")
    if status not in (0, None):
        err = "unauthorized" if status in (1004, 1008) else "unreachable"
        result = _empty(err, now)
        _cache = (now, result)
        return result

    model = _pick_model((body or {}).get("model_remains") or [])
    if not model:
        result = _empty("unreachable", now)
        _cache = (now, result)
        return result

    result = {
        "five_hour": {
            "used_percentage": _used(model.get("current_interval_remaining_percent")),
            "reset_at": _ms_to_epoch(model.get("end_time")),
        },
        "seven_day": {
            "used_percentage": _used(model.get("current_weekly_remaining_percent")),
            "reset_at": _ms_to_epoch(model.get("weekly_end_time")),
        },
        "fetched_at": now,
        "error": None,
    }
    _cache = (now, result)
    return result


def _now() -> float:
    import time

    return time.time()
