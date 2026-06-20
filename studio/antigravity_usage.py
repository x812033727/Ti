"""Antigravity 訂閱額度查詢 —— 透過 Google Code Assist 後端取得每模型請求配額。

Antigravity（`agy`）走 Google OAuth（Gemini Code Assist）。其 `/usage` 顯示的每模型請求配額
底層是 ``cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota``，回傳每個模型的 REQUESTS
配額：``remainingFraction``（1=剩 100%）與 ``resetTime``（ISO8601）。

**關鍵**：retrieveUserQuota 的 request body 必須帶 ``{"project": <cloudaicompanionProject>}``
（取自 loadCodeAssist 回應）——空 body `{}` 會 403「no valid license」。故本模組兩步：
先 loadCodeAssist 取 project（順帶 currentTier/paidTier），再 retrieveUserQuota 帶 project 取
buckets。有數值 buckets 就像 claude/codex 畫每模型百分比條；若該帳號層級無數值配額（例如
retrieveUserQuota 仍 403）則 fallback 顯示訂閱層級。

token 取自 agy 維護的 ``~/.gemini/antigravity-cli/antigravity-oauth-token``（agy 執行時刷新）。
access_token 約每小時過期、僅在 agy 跑時刷新——過期（401）時回 unauthorized，跑一次
Antigravity 討論即恢復（不自行 refresh，避免動用內嵌 OAuth client secret）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx

from . import config

QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
TIER_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
_TIMEOUT = 8.0
_TTL = 60.0
_MAX_BUCKETS = 12

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


def _token_expired(now: float) -> bool:
    """token 檔的 access_token 是否已過期（token.expiry 為 ISO8601）。讀不到視為未過期。

    agy token 約每小時過期、僅在 agy 跑時刷新；過期時打 Code Assist 端點必得 401。
    先短路省一次註定失敗的 HTTP，並讓前端拿到明確「過期」訊息而非空白。
    """
    try:
        data = json.loads(config.ANTIGRAVITY_OAUTH_TOKEN_FILE.read_text(encoding="utf-8"))
        exp = _iso_to_epoch((data.get("token") or {}).get("expiry"))
    except (OSError, json.JSONDecodeError):
        return False
    return exp is not None and now >= exp


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


def _result(now: float, *, buckets=None, tier=None, error=None) -> dict:
    return {"buckets": buckets or [], "tier": tier, "fetched_at": now, "error": error}


def _post(token: str, url: str, body: dict) -> tuple[int, Any]:
    """POST；回 (status_code, parsed_json_or_None)。網路錯誤 → status=-1。"""
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        return -1, None
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def fetch_rate_limits(force: bool = False) -> dict:
    """查 Antigravity 每模型請求配額（或訂閱層級）。回傳正規化 dict（含 error，永不拋例外）。

    error ∈ {None, "token_missing", "unauthorized", "unreachable"}。
    buckets: [{label, used_percentage, reset_at(epoch)}]，依 used_percentage 由高到低（有數值配額時）。
    tier: {label, tier_id, unlimited, paid_tier} 或 None。
    """
    global _cache
    now = _now()
    if not force and _cache is not None and now - _cache[0] < _TTL:
        return _cache[1]

    token = _read_token()
    if not token:
        return _store(now, _result(now, error="token_missing"))

    if _token_expired(now):
        # 已知過期：直接回 unauthorized，前端顯示「token 已過期，跑一次討論即恢復」。
        return _store(now, _result(now, error="unauthorized"))

    # 1) loadCodeAssist：取 cloudaicompanionProject（retrieveUserQuota 必需）與層級。
    lstatus, lbody = _post(token, TIER_URL, {})
    if lstatus == -1:
        return _store(now, _result(now, error="unreachable"))
    if lstatus in (401, 403):
        return _store(now, _result(now, error="unauthorized"))
    if lstatus != 200 or not isinstance(lbody, dict):
        return _store(now, _result(now, error="unreachable"))
    project = lbody.get("cloudaicompanionProject")
    tier = _parse_tier(lbody)

    # 2) retrieveUserQuota（帶 project）→ 每模型 buckets。
    if isinstance(project, str) and project:
        qstatus, qbody = _post(token, QUOTA_URL, {"project": project})
        if qstatus == 200:
            buckets = _parse_buckets(qbody)
            if buckets:
                return _store(now, _result(now, buckets=buckets, tier=tier))
        elif qstatus == 401:
            return _store(now, _result(now, error="unauthorized"))
        # 403 / 空 buckets / 其他 → 落到層級顯示（此層級無數值配額）。

    # 3) Fallback：顯示訂閱層級。
    return _store(now, _result(now, tier=tier))


def _store(now: float, result: dict) -> dict:
    global _cache
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


def _parse_tier(body: Any) -> dict | None:
    """loadCodeAssist 回應 → {label, tier_id, unlimited, paid_tier} 或 None。"""
    if not isinstance(body, dict):
        return None
    cur = body.get("currentTier")
    if not isinstance(cur, dict):
        return None
    name = cur.get("name")
    if not isinstance(name, str) or not name:
        return None
    desc = cur.get("description")
    unlimited = isinstance(desc, str) and "unlimited" in desc.lower()
    paid = body.get("paidTier")
    paid_name = paid.get("name") if isinstance(paid, dict) else None
    return {
        "label": name,
        "tier_id": cur.get("id") if isinstance(cur.get("id"), str) else None,
        "unlimited": unlimited,
        "paid_tier": paid_name if isinstance(paid_name, str) and paid_name else None,
    }


def _now() -> float:
    import time

    return time.time()
