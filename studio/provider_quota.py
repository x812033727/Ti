"""Provider 即時額度快照與「給 PM 的額度摘要」。

從 `routes.py` 抽出（`snapshot()` ＝原 `_provider_quota_snapshot`、`_antigravity_status` 原樣搬來），
讓 orchestrator 的動態分派也能取用同一份聚合——**routes 會 import orchestrator 鏈，故快照不可
留在 routes 由 orchestrator 反向 import**（會循環）。本模組只依賴 config 與各 usage 模組。

各 provider 的即時剩餘額度由 `studio/{claude,codex,minimax,antigravity}_usage.py:fetch_rate_limits()`
查官方端點（皆 60 秒記憶體快取）。`snapshot()` 內含阻塞 I/O，呼叫端應 `asyncio.to_thread` 包起來。

`snapshot()` 另有模組級 stale-while-revalidate（SWR）快取：快取新鮮直接回；過期但未超過
`config.QUOTA_STALE_MAX` 則**立即回舊快照（附 ``stale: true``）**並由背景執行緒單飛刷新——
設定面板、orchestrator 派工前與 autopilot 額度閘門等關鍵路徑不再同步等最慢 provider；
無快取或太舊才同步查（首次啟動仍正確）。詳見 ``snapshot()`` docstring。

給混合模式動態分派用的衍生 helper：
- ``summarize_for_pm(snap, role_provider_map)``：精簡人讀摘要（每 provider 用量%/重置倒數/就緒，
  標注哪些角色用它），塞進 PM 的動態 step prompt，讓 PM 依「目前額度」分派/招募。
- ``constrained(snap, provider)``：該 provider 是否受限（未就緒/查詢異常/用量達門檻）。
- ``least_constrained_ready(snap)``：就緒且最寬鬆的 provider key（受限角色自動重綁的安全網）。
- ``gate(snap)``：全域額度閘門 ``(any_usable, earliest_reset_epoch)``（autopilot 主迴圈節流用）。
- ``digest(snap)``：壓成 ``{provider: {ready, error, max_used, soonest_reset}}`` plain dict，
  給 flow.choose_dispatch（純函式層不 import 本模組，由 orchestrator 傳參）。
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
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

log = logging.getLogger("ti.provider_quota")

# 受限門檻：任一額度窗用量達此百分比即視為「受限」（觸發自動重綁/PM 避用）。
CONSTRAINED_THRESHOLD = 90.0

# --- snapshot 的模組級 SWR 快取 ------------------------------------------------
# 各 usage 模組已有 60s TTL 快取，但快取一過期，snapshot() 呼叫端就得同步等「最慢一家」
# provider 查完（實測 ~1.3s）。此處在 snapshot 層再加一層 stale-while-revalidate：
# 過期但未超過 config.QUOTA_STALE_MAX 的舊快照先回（附 stale=true），刷新丟背景執行緒
# 單飛跑，關鍵路徑（設定面板/派工/額度閘門）不再白等。
_TTL = 60.0  # 快照「新鮮」秒數：與各 usage 模組的 60s TTL 對齊
_lock = threading.Lock()  # 保護 _cache 與 _refresh_thread（單飛 flag）的讀寫
_cache: tuple[float, dict] | None = None  # (fetched_at, snapshot)
_refresh_thread: threading.Thread | None = None  # 進行中的背景刷新；None＝沒有在跑


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


# 單一 provider 額度探測的硬上限(秒):見 _fetch 內註解。
_PROBE_TIMEOUT_S = 30.0


def _fetch() -> dict:
    """同步聚合各 provider 的「即時剩餘額度」（阻塞 I/O，耗時＝最慢一家 provider）。

    僅保留 Claude / Codex CLI / Antigravity CLI / MiniMax 四個 provider；各自向官方端點
    即時查剩餘配額（claude/codex/minimax/antigravity_usage，皆 60 秒快取）。
    對外請走 ``snapshot()``（帶 SWR 快取）；本函式只給 snapshot 的同步路徑與背景刷新用。
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
    # 每個探測 result 都設硬上限(_PROBE_TIMEOUT_S):各 provider 模組內部雖各有 8-12s
    # httpx timeout,但只要日後任一探測漏掉,無上限的 f.result() 會讓 snapshot() 在
    # autopilot 主迴圈(quota gate)無限卡死且無 log——主迴圈「任務之間」沒有看門狗,
    # 這類阻塞等於整台停擺(2026-07-10 主迴圈盲區調查結論)。逾時該家記 None(=不可用,
    # 呼叫端本就容錯),executor 以 shutdown(wait=False, cancel_futures=True) 非阻塞退出,
    # 不等卡死的探測執行緒。
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=4 + len(claude_accts))
    try:
        f_claude = ex.submit(claude_usage.fetch_rate_limits) if claude_on else None
        f_codex = ex.submit(codex_usage.fetch_rate_limits) if codex_on else None
        f_minimax = ex.submit(minimax_usage.fetch_rate_limits) if minimax_on else None
        f_agy = ex.submit(_antigravity_status)
        f_accts = [
            (a, ex.submit(claude_usage.fetch_rate_limits, cred_file=Path(a["cred_file"])))
            for a in claude_accts
        ]

        def _bounded(fut, label: str):
            if fut is None:
                return None
            try:
                return fut.result(timeout=_PROBE_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                log.warning(
                    "provider 額度探測逾時(%ss):%s——本輪視為不可用", _PROBE_TIMEOUT_S, label
                )
                return None
            except Exception:  # noqa: BLE001 — 單一探測失敗不得拖垮整份快照
                log.debug("provider 額度探測失敗:%s", label, exc_info=True)
                return None

        claude_rl = _bounded(f_claude, "claude")
        codex_rl = _bounded(f_codex, "codex")
        minimax_rl = _bounded(f_minimax, "minimax")
        agy_status = _bounded(f_agy, "antigravity") or {}
        claude_accounts_out = [
            {
                "label": a["label"],
                "subscription": a.get("subscription"),
                "active": a.get("active", False),
                "pinned": a.get("pinned", False),
                "rate_limits": _account_rate_limits(a, _bounded(fut, f"claude:{a['label']}")),
            }
            for a, fut in f_accts
        ]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
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
            # 帳號分配模式：manual＝使用者釘選（pin 檔存在，凍結自動輪替）；auto＝v4 政策
            # 自動輪替。enabled 反映 TI_CLAUDE_ROTATE 開關，前端據此渲染模式列。
            "rotate": {
                "enabled": config.CLAUDE_ROTATE,
                "mode": "manual" if (_pinned := claude_accounts.pinned_label()) else "auto",
                "pinned": _pinned,
            },
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
        # SWR 標記：同步查得的快照必為新鮮；stale 路徑會在複本上蓋成 True。
        "stale": False,
        "providers": providers,
    }


def _refresh_in_background() -> None:
    """背景刷新 worker：成功則更新快取；失敗保留舊快照並記 log，**絕不拋出**。"""
    global _cache, _refresh_thread
    try:
        snap = _fetch()
        with _lock:
            _cache = (time.time(), snap)
    except Exception:  # noqa: BLE001 — 背景執行緒無人接例外，失敗只記 log、沿用舊快照
        log.exception("provider 額度背景刷新失敗，沿用舊快照")
    finally:
        with _lock:
            # 只清掉自己（防極端時序下誤清新一輪刷新的 flag），讓下一次 stale 呼叫可再觸發。
            if _refresh_thread is threading.current_thread():
                _refresh_thread = None


def _spawn_refresh_locked() -> None:
    """觸發一次背景刷新（呼叫端須已持 ``_lock``）；已有刷新在跑則跳過（單飛）。"""
    global _refresh_thread
    if _refresh_thread is not None and _refresh_thread.is_alive():
        return
    t = threading.Thread(target=_refresh_in_background, name="ti-quota-refresh", daemon=True)
    _refresh_thread = t
    t.start()


def _reset_cache() -> None:
    """清空模組級快照快取與單飛狀態（測試隔離用；生產不需呼叫）。"""
    global _cache, _refresh_thread
    with _lock:
        _cache = None
        _refresh_thread = None


def snapshot() -> dict:
    """回 provider 額度快照，帶 stale-while-revalidate（SWR）模組級快取。

    - 快取新鮮（年齡 < 60s）→ 直接回快取。
    - 快取過期但年齡未超過 ``config.QUOTA_STALE_MAX``（預設 300s；0＝停用）→ **立即回舊
      快照的複本**，附 ``stale: true``（``updated_at`` 仍為舊值，前端可顯示「更新中…」），
      同時由背景 daemon 執行緒單飛刷新（同時只允許一個在跑），完成後更新快取。
    - 無快取或超過 STALE_MAX → 同步查（首次啟動／久未使用仍拿到正確資料）。

    回傳結構與舊版相同，僅新增 ``stale`` 欄位；``gate()``/``digest()``/``constrained()``
    等下游零改動。同步路徑仍含阻塞 I/O，呼叫端照舊以 ``asyncio.to_thread`` 包起來。
    """
    global _cache
    now = time.time()
    with _lock:
        if _cache is not None:
            age = now - _cache[0]
            if age < _TTL:
                return _cache[1]
            if age < config.QUOTA_STALE_MAX:
                _spawn_refresh_locked()
                # 回複本，不就地改動快取物件（背景刷新完成前，快取仍是這份舊快照）。
                return {**_cache[1], "stale": True}
    snap = _fetch()
    with _lock:
        _cache = (time.time(), snap)
    return snap


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
        w["used_percentage"] for w in windows if isinstance(w.get("used_percentage"), int | float)
    ]
    resets = [w["reset_at"] for w in windows if isinstance(w.get("reset_at"), int | float)]
    return {
        "ready": ready,
        "error": error,
        "max_used": max(used) if used else None,
        "soonest_reset": min(resets) if resets else None,
    }


def digest(snap: dict) -> dict:
    """把 snapshot 壓成 ``{provider: {ready, error, max_used, soonest_reset}}`` 的 plain dict。

    供 flow.choose_dispatch（純函式決策層）消費——flow 不得 import 本模組，由 orchestrator
    查快照後把 digest 當參數傳入。條目非 dict／缺 key 者略過；空或壞快照回空 dict。
    """
    out: dict[str, dict] = {}
    for entry in (snap or {}).get("providers", []):
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if key:
            out[key] = _usage(entry)
    return out


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
    """就緒、未受限且最寬鬆（用量最低、無 error）的 provider key；都不可用回 None。

    受限角色自動重綁的安全網——把工作導到還有額度的 provider，避免限流空轉。
    """
    best: str | None = None
    best_used: float | None = None
    for entry in snap.get("providers", []):
        key = entry.get("key")
        if not key or constrained(snap, key):
            continue
        u = _usage(entry)
        used = u["max_used"] if u["max_used"] is not None else 0.0
        if best_used is None or used < best_used:
            best, best_used = key, used
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
        # 按模型 scoped 的專屬限額（如 Fable 週限）：與全域窗獨立，附在同一行讓 PM 依
        # 「某模型快滿」把重任務改派其他模型，而非誤判整家 provider 受限。
        for mname, mw in ((entry.get("rate_limits") or {}).get("models") or {}).items():
            if not isinstance(mw, dict):
                continue
            mpct = mw.get("used_percentage")
            if mpct is None:
                continue
            mwarn = "⚠️" if mpct >= CONSTRAINED_THRESHOLD else ""
            seg = f"{mname} 模型限額 {mwarn}{mpct:.0f}%"
            if isinstance(mw.get("reset_at"), int | float):
                mins = max(0, int((mw["reset_at"] - now) / 60))
                seg += (
                    f"（約 {mins // 60} 小時後重置）" if mins >= 120 else f"（約 {mins} 分後重置）"
                )
            parts.append(seg)
        lines.append(f"- {key}{who}：{'、'.join(parts) if parts else 'ready'}")
    return "\n".join(lines)
