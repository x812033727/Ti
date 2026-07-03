"""Provider 即時額度快照與「給 PM 的額度摘要」。

從 `routes.py` 抽出（`snapshot()` ＝原 `_provider_quota_snapshot`、`_antigravity_status` 原樣搬來），
讓 orchestrator 的動態分派也能取用同一份聚合——**routes 會 import orchestrator 鏈，故快照不可
留在 routes 由 orchestrator 反向 import**（會循環）。本模組只依賴 config 與各 usage 模組。

各 provider 的即時剩餘額度由 `studio/{claude,codex,minimax,antigravity}_usage.py:fetch_rate_limits()`
查官方端點（皆 60 秒記憶體快取）。`snapshot()` 內含阻塞 I/O，呼叫端應 `asyncio.to_thread` 包起來。

給混合模式動態分派用的衍生 helper：
- ``summarize_for_pm(snap, role_provider_map)``：精簡人讀摘要（每 provider 用量%/重置倒數/就緒，
  標注哪些角色用它），塞進 PM 的動態 step prompt，讓 PM 依「目前額度」分派/招募。
- ``constrained(snap, provider)``：該 provider 是否受限（未就緒/查詢異常/用量達門檻）。
- ``least_constrained_ready(snap)``：就緒且最寬鬆的 provider key（受限角色自動重綁的安全網）。
- ``gate(snap)``：全域額度閘門 ``(any_usable, earliest_reset_epoch)``（autopilot 主迴圈節流用）。
"""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

from . import (
    antigravity_usage,
    claude_accounts,
    claude_usage,
    codex_usage,
    config,
    minimax_usage,
)

# 受限門檻：任一額度窗用量達此百分比即視為「受限」（觸發自動重綁/PM 避用）。
CONSTRAINED_THRESHOLD = 90.0


def _antigravity_status() -> dict:
    available = config.antigravity_cli_available()
    # 過去用 `agy models`（12s 子程序）判 signed_in，且只在它成功時才附 rate_limits。問題：
    # agy token 約每小時過期、僅在 agy 跑時刷新，token 一過期 `agy models` 就失敗 →
    # signed_in=False → rate_limits=None → 前端整區「空白」（非顯示明確過期訊息），且每次
    # snapshot 都付 12s 子程序成本、狀態跟著 flapping。改為：binary 在就直接查 rate_limits，
    # 由其 error 欄位（token_missing / unauthorized）表達 token 狀態，前端永遠有話可說。
    rate_limits = antigravity_usage.fetch_rate_limits() if available else None
    rl_error = (rate_limits or {}).get("error")
    has_token = available and rl_error != "token_missing"
    signed_in = available and rl_error is None
    if not available:
        detail = "未安裝 Antigravity CLI（`agy`）。"
    elif signed_in:
        detail = "Google Code Assist 配額已即時查得。"
    elif rl_error == "token_missing":
        detail = "尚未登入 `agy`（找不到 OAuth token）。"
    else:
        detail = "Antigravity token 已過期，跑一次 Antigravity 討論即自動刷新。"
    return {
        "key": "antigravity",
        "label": "Antigravity CLI",
        "active": config.PROVIDER == "antigravity",
        # 有 token（即使過期、可由跑討論刷新）即視為 ready；只有完全沒登入才算未就緒。
        "ready": has_token,
        "status": "ok" if signed_in else ("warn" if available else "missing"),
        "auth": "signed_in" if has_token else "needs_login",
        "binary": config.ANTIGRAVITY_BIN,
        "models": [],
        "quota": {
            "kind": "subscription",
            "summary": "可用訂閱/帳號額度" if has_token else "需要先登入 `agy`",
            "detail": detail,
        },
        # 一律附 rate_limits（含 error 欄位）；前端據 error 顯示明確訊息而非空白。
        "rate_limits": rate_limits,
    }


def _account_rate_limits(acct: dict, rl: dict | None) -> dict | None:
    """非在線帳號的 ``unauthorized`` 改映射為 ``stale_label``（誠實顯示、不誤導）。

    非在線帳號的標籤檔本來就不會被 CLI 續期，token 過期只代表「快照舊了」——額度本身
    不受影響，切換到該帳號一次即刷新；照抄 unauthorized 會讓使用者誤以為帳號登出。
    在線帳號的 error 保留原值（真 unauthorized 就該顯示）。回新 dict，不改動
    claude_usage 的 TTL 快取物件。
    """
    if rl is None or acct.get("active", False) or rl.get("error") != "unauthorized":
        return rl
    return {**rl, "error": "stale_label"}


def snapshot() -> dict:
    """只回各 provider 的「即時剩餘額度」（官方 rate limit），不含 Ti 本機累積用量。

    僅保留 Claude / Codex CLI / Antigravity CLI / MiniMax 四個 provider；各自向官方端點
    即時查剩餘配額（claude/codex/minimax/antigravity_usage，皆 60 秒快取）。
    """
    now = time.time()
    claude_on = config.claude_cli_logged_in() and not config.has_api_key()
    codex_on = (
        config.codex_cli_available() and config.codex_cli_logged_in() and not config.CODEX_API_KEY
    )
    minimax_on = bool(config.MINIMAX_API_KEY)
    # 多帳號：claude 走訂閱時列出本機憑證標籤檔（acct-A/acct-B…），各帳號獨立查額度，
    # 讓設定頁同時顯示並可切換。無標籤檔（單帳號舊機）則回 []，前端退回單一額度顯示。
    # 讀之前先把線上憑證（CLI 自動續期後最新）回寫在線 label 標籤檔，否則長期不切換時
    # 標籤檔 expiresAt 過期 → 在線帳號額度誤顯示 unauthorized。同步失敗不得拖垮快照。
    try:
        claude_accounts.sync_active_label()
    except Exception:
        pass
    claude_accts = claude_accounts.list_accounts() if claude_on else []
    # 各 rate-limit 查詢彼此獨立、皆為阻塞 I/O（各 60s 快取）；並行跑使端點延遲取「最慢
    # 一家」而非「總和」，配合 antigravity 已移除 12s 子程序，明顯改善前端轉圈。
    with concurrent.futures.ThreadPoolExecutor(max_workers=4 + len(claude_accts)) as ex:
        f_claude = ex.submit(claude_usage.fetch_rate_limits) if claude_on else None
        f_codex = ex.submit(codex_usage.fetch_rate_limits) if codex_on else None
        f_minimax = ex.submit(minimax_usage.fetch_rate_limits) if minimax_on else None
        f_agy = ex.submit(_antigravity_status)
        f_accts = [
            (a, ex.submit(claude_usage.fetch_rate_limits, cred_file=Path(a["cred_file"])))
            for a in claude_accts
        ]
        claude_rl = f_claude.result() if f_claude else None
        codex_rl = f_codex.result() if f_codex else None
        minimax_rl = f_minimax.result() if f_minimax else None
        agy_status = f_agy.result()
        claude_accounts_out = [
            {
                "label": a["label"],
                "subscription": a.get("subscription"),
                "active": a.get("active", False),
                "rate_limits": _account_rate_limits(a, fut.result()),
            }
            for a, fut in f_accts
        ]
    providers = [
        {
            "key": "claude",
            "label": "Claude",
            "active": config.PROVIDER == "claude",
            "ready": config.has_api_key() or config.claude_cli_logged_in(),
            "status": "ok"
            if (config.has_api_key() or config.claude_cli_logged_in())
            else "missing",
            "auth": "api_key"
            if config.has_api_key()
            else ("oauth" if config.claude_cli_logged_in() else "missing"),
            "quota": {
                "kind": "subscription_or_api",
                "summary": "Claude 訂閱剩餘額度",
                "detail": "由 Anthropic 官方 usage 端點即時查詢（每 60 秒快取一次）。",
            },
            # 訂閱（OAuth 登入、非 API key）時附官方 rate limit；否則 None（前端不顯示）。
            "rate_limits": claude_rl,
            # 多帳號額度與在線標記；單帳號（無標籤檔）時為 []，前端退回上面的單一 rate_limits。
            "accounts": claude_accounts_out,
        },
        {
            "key": "codex",
            "label": "Codex CLI",
            "active": config.PROVIDER == "codex",
            "ready": config.codex_cli_available() and config.codex_cli_logged_in(),
            "status": "ok"
            if (config.codex_cli_available() and config.codex_cli_logged_in())
            else ("warn" if config.codex_cli_available() else "missing"),
            "auth": "api_key"
            if config.CODEX_API_KEY
            else ("oauth" if config.codex_cli_logged_in() else "missing"),
            "binary": config.CODEX_BIN,
            "quota": {
                "kind": "subscription_or_api",
                "summary": "Codex 訂閱剩餘額度",
                "detail": "由 codex app-server 即時查詢（每 60 秒快取一次）。",
            },
            "rate_limits": codex_rl,
        },
        agy_status,
        {
            "key": "minimax",
            "label": "MiniMax",
            "active": config.PROVIDER == "minimax",
            "ready": bool(config.MINIMAX_API_KEY),
            "status": "ok" if config.MINIMAX_API_KEY else "missing",
            "auth": "api_key" if config.MINIMAX_API_KEY else "missing",
            "quota": {
                "kind": "api",
                "summary": "MiniMax 訂閱剩餘額度",
                "detail": "由 MiniMax token_plan/remains 端點即時查詢（每 60 秒快取一次）。",
            },
            "rate_limits": minimax_rl,
        },
    ]

    return {
        "ok": True,
        "active_provider": config.PROVIDER,
        "provider_ready": config.provider_ready(),
        "updated_at": now,
        "providers": providers,
    }


# --- 給混合模式動態分派的衍生 helper ----------------------------------------


def _by_key(snap: dict, provider: str) -> dict | None:
    for entry in snap.get("providers", []):
        if entry.get("key") == provider:
            return entry
    return None


def _usage(entry: dict) -> dict:
    """從 provider 快照條目抽出 ``{ready, error, max_used, soonest_reset}``。

    相容兩種 rate_limits 結構：window 式（claude/codex/minimax 的 five_hour/seven_day…）與
    bucket 式（antigravity 的 ``buckets: [{used_percentage, reset_at}]``）。
    """
    ready = bool(entry.get("ready"))
    rl = entry.get("rate_limits") or {}
    error = rl.get("error")
    if isinstance(rl.get("buckets"), list):
        windows = [w for w in rl["buckets"] if isinstance(w, dict)]
    else:
        windows = [v for v in rl.values() if isinstance(v, dict) and "used_percentage" in v]
    used = [
        w["used_percentage"] for w in windows if isinstance(w.get("used_percentage"), (int, float))
    ]
    resets = [w["reset_at"] for w in windows if isinstance(w.get("reset_at"), (int, float))]
    return {
        "ready": ready,
        "error": error,
        "max_used": max(used) if used else None,
        "soonest_reset": min(resets) if resets else None,
    }


def constrained(snap: dict, provider: str, threshold: float = CONSTRAINED_THRESHOLD) -> bool:
    """provider 是否受限：找不到/未就緒/查詢異常/任一額度窗用量達門檻 → True。"""
    entry = _by_key(snap, provider)
    if entry is None:
        return True
    u = _usage(entry)
    if not u["ready"] or u["error"]:
        return True
    return u["max_used"] is not None and u["max_used"] >= threshold


def least_constrained_ready(snap: dict) -> str | None:
    """就緒且最寬鬆（用量最低、無 error）的 provider key；都不可用回 None。

    受限角色自動重綁的安全網——把工作導到還有額度的 provider，避免限流空轉。
    """
    best: str | None = None
    best_used: float | None = None
    for entry in snap.get("providers", []):
        u = _usage(entry)
        if not u["ready"] or u["error"]:
            continue
        used = u["max_used"] if u["max_used"] is not None else 0.0
        if best_used is None or used < best_used:
            best, best_used = entry.get("key"), used
    return best


def gate(snap: dict, threshold: float = CONSTRAINED_THRESHOLD) -> tuple[bool, float | None]:
    """全域額度閘門：回 ``(any_usable, earliest_reset_epoch)``，供 autopilot 主迴圈節流。

    any_usable＝至少一個 provider「可用」：ready、無 error、且 ``max_used`` 低於受限門檻
    （複用 ``constrained()`` 的 ``CONSTRAINED_THRESHOLD``，勿另造門檻）；拿不到用量資訊
    （``max_used is None``）視為可用，與 ``constrained()`` 的判定對齊。

    earliest_reset_epoch＝「就緒且無 error、但用量達門檻」的 provider 中最早的 reset_at
    （epoch 秒）——只有這類 provider 會在重置後重新變可用，未就緒/查詢異常者的 reset 不算數。
    全無 reset 資訊回 None，呼叫端自行套睡眠下限/上限。
    """
    any_usable = False
    resets: list[float] = []
    for entry in snap.get("providers", []):
        u = _usage(entry)
        if not u["ready"] or u["error"]:
            continue
        if u["max_used"] is None or u["max_used"] < threshold:
            any_usable = True
        elif u["soonest_reset"] is not None:
            resets.append(u["soonest_reset"])
    return any_usable, (min(resets) if resets else None)


def summarize_for_pm(snap: dict, role_provider_map: dict[str, str] | None = None) -> str:
    """精簡人讀摘要：每 provider 一行（用量%/重置倒數/就緒），標注哪些角色用它。

    role_provider_map: ``{role_key: provider_key}``（本場各角色實際綁的 provider），用來在每個
    provider 後標「（pm、qa 用）」，讓 PM 一眼看出「找誰＝用哪家額度」。空摘要回空字串。
    """
    by_prov: dict[str, list[str]] = {}
    for rk, pk in (role_provider_map or {}).items():
        by_prov.setdefault(pk, []).append(rk)
    now = snap.get("updated_at") or time.time()
    lines: list[str] = []
    for entry in snap.get("providers", []):
        key = entry.get("key", "")
        users = "、".join(by_prov.get(key, []))
        who = f"（{users} 用）" if users else ""
        u = _usage(entry)
        if not u["ready"]:
            lines.append(f"- {key}{who}：未就緒/不可用")
            continue
        if u["error"]:
            lines.append(f"- {key}{who}：額度查詢異常（{u['error']}）")
            continue
        parts: list[str] = []
        if u["max_used"] is not None:
            warn = "⚠️" if u["max_used"] >= CONSTRAINED_THRESHOLD else ""
            parts.append(f"{warn}用量 {u['max_used']:.0f}%")
        if u["soonest_reset"]:
            mins = max(0, int((u["soonest_reset"] - now) / 60))
            parts.append(f"約 {mins} 分後重置")
        lines.append(f"- {key}{who}：{'、'.join(parts) if parts else 'ready'}")
    return "\n".join(lines)
