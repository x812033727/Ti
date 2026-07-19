"""Codex 訂閱額度查詢 —— 透過 `codex app-server` 的 JSON-RPC 取得 rate limit。

Codex CLI 沒有簡單的 HTTP 額度端點，但其 app-server（stdio JSON-RPC）支援
``account/rateLimits/read``：握手後問一句即回 primary（5 小時窗，windowDurationMins=300）
與 secondary（週窗，10080）的 usedPercent 與 resetsAt（epoch 秒）。

正規化成與 claude_usage 相同的形狀（five_hour / seven_day + used_percentage + reset_at），
讓前端 rateLimitBlock 直接重用。結果以模組級記憶體 TTL 快取，避免反覆 spawn app-server。
"""

from __future__ import annotations

import json
import select
import subprocess

from . import config

_TTL = 60.0  # 快取秒數：app-server spawn 較重，60s 內重複查直接回上次結果
_READ_DEADLINE = 12.0  # 讀 stdout 等 id==2 回應的上限秒數

# (fetched_at, result)；程序生命週期內共用，重啟即清空。
_cache: tuple[float, dict] | None = None

_INIT_REQ = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize",'
    '"params":{"clientInfo":{"name":"ti-status","version":"1.0"}}}'
)
_RATELIMITS_REQ = '{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}'


def _window(d) -> dict | None:
    """primary/secondary → {used_percentage, reset_at(epoch)}；非 dict 回 None。"""
    if not isinstance(d, dict):
        return None
    pct = d.get("usedPercent")
    reset = d.get("resetsAt")
    return {
        "used_percentage": round(float(pct), 1) if isinstance(pct, int | float) else None,
        "reset_at": float(reset) if isinstance(reset, int | float) else None,
    }


def _empty(error: str, now: float) -> dict:
    return {"five_hour": None, "seven_day": None, "fetched_at": now, "error": error}


def _read_rate_limits() -> dict | None:
    """Spawn `codex app-server`，送握手 + rateLimits 請求，讀到 id==2 即回 rateLimits dict。

    讀不到 / spawn 失敗回 None。永不拋例外（呼叫端據此回 unreachable）。
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 - 固定 argv，bin 來自 config
            [config.CODEX_BIN, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except (OSError, ValueError):
        return None

    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(_INIT_REQ + "\n")
        proc.stdin.write(_RATELIMITS_REQ + "\n")
        proc.stdin.flush()

        import time as _time

        deadline = _time.monotonic() + _READ_DEADLINE
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                return None
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                return (msg.get("result") or {}).get("rateLimits")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def fetch_rate_limits(force: bool = False) -> dict:
    """查 Codex 訂閱 rate limit。回傳正規化 dict（含 error 欄位，永不拋例外）。

    error ∈ {None, "unreachable"}。force=True 繞過 TTL 快取。
    """
    global _cache
    now = _now()
    if not force and _cache is not None and now - _cache[0] < _TTL:
        return _cache[1]

    rl = _read_rate_limits()
    if not isinstance(rl, dict):
        result = _empty("unreachable", now)
        _cache = (now, result)
        return result

    result = {
        "five_hour": _window(rl.get("primary")),
        "seven_day": _window(rl.get("secondary")),
        "fetched_at": now,
        "error": None,
    }
    _cache = (now, result)
    return result


def _now() -> float:
    import time

    return time.time()
