"""Antigravity 訂閱額度查詢 —— 透過 Google Code Assist 後端取得每模型請求配額。

Antigravity（`agy`）走 Google OAuth（Gemini Code Assist）。其 `/usage` 是互動 TUI 限定
指令，但底層是打 ``cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota``，回傳每個
模型的 REQUESTS 配額：``remainingFraction``（1=剩 100%）與 ``resetTime``（ISO8601）。

token 取自 agy 維護的 ``~/.gemini/antigravity-cli/antigravity-oauth-token``（agy 執行時刷新）。
與 claude/codex 不同，這裡是「每模型 bucket」而非時間窗，故正規化成 buckets 清單，前端
另以列表呈現。access_token 約每小時過期、僅在 agy 跑時刷新——過期時回 unauthorized，
跑一次 Antigravity 討論即恢復（不自行 refresh，避免動用內嵌 OAuth client secret）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx

from . import config

QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
_TIMEOUT = 8.0
_TTL = 60.0
_MAX_BUCKETS = 8

# (fetched_at, result)；程序生命週期內共用，重啟即清空。
_cache: tuple[float, dict] | None = None


def _read_token() -> str | None:
    """從 agy 的 oauth token 檔讀 access_token；缺檔/壞檔/無 token 回 None。"""
    try:
        data = json.loads(config.ANTIGRAVITY_OAUTH_TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tok = (data.get("token") or {}).get("access_token")
    return tok if isinstance(tok, str) and tok else None


def _iso_to_epoch(s: Any) -> float | None:
    """ISO8601 → unix 秒；容忍 9 位奈秒小數（截到 6 位）與結尾 Z。失敗回 None。"""
    if not isinstance(s, str) or not s:
        return None
    txt = s.replace("Z", "+00:00")
    # 把過長的小數秒（>6 位）截成微秒，否則 fromisoformat 會炸
    txt = re.sub(r"(\.\d{6})\d+", r"\1", txt)
    try:
        return datetime.fromisoformat(txt).timestamp()
    except ValueError:
        return None


def _prettify(model_id: str) -> str:
    """gemini-2.5-pro → Gemini 2.5 Pro（純數字段保留原樣）。"""
    return " ".join(w.title() if w.isalpha() else w for w in model_id.split("-"))


def _empty(error: str, now: float) -> dict:
    return {"buckets": [], "fetched_at": now, "error": error}


def fetch_rate_limits(force: bool = False) -> dict:
    """查 Antigravity 每模型請求配額。回傳正規化 dict（含 error，永不拋例外）。

    error ∈ {None, "token_missing", "unauthorized", "unreachable"}。
    buckets: [{label, used_percentage, reset_at(epoch)}]，依 used_percentage 由高到低。
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

    try:
        resp = httpx.post(
            QUOTA_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
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

    result = {"buckets": _parse_buckets(body), "fetched_at": now, "error": None}
    _cache = (now, result)
    return result


def _parse_buckets(body: Any) -> list[dict]:
    """retrieveUserQuota 回應 → [{label, used_percentage, reset_at}]，REQUESTS 型、依用量降序。"""
    out: list[dict] = []
    seen: set[str] = set()
    for b in (body or {}).get("buckets", []) if isinstance(body, dict) else []:
        if not isinstance(b, dict) or b.get("tokenType") != "REQUESTS":
            continue
        model = b.get("modelId")
        if not isinstance(model, str) or model in seen:
            continue
        seen.add(model)
        frac = b.get("remainingFraction")
        used = round((1 - float(frac)) * 100, 1) if isinstance(frac, (int, float)) else None
        out.append(
            {
                "label": _prettify(model),
                "used_percentage": used,
                "reset_at": _iso_to_epoch(b.get("resetTime")),
            }
        )
    out.sort(
        key=lambda x: (x["used_percentage"] is not None, x["used_percentage"] or 0), reverse=True
    )
    return out[:_MAX_BUCKETS]


def _now() -> float:
    import time

    return time.time()
