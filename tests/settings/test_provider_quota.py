"""Provider quota/status endpoint tests（只回即時剩餘額度，4 個 provider，無歷史用量）。

後半為 ``snapshot()`` 模組級 SWR（stale-while-revalidate）快取的行為測試：新鮮直回、
過期回 stale 快照＋背景單飛刷新、超過 STALE_MAX 同步查、刷新失敗保舊快照。
測試間的快取隔離由 tests/conftest.py 的 ``_reset_provider_quota_cache`` autouse fixture 保證。
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time

import pytest
from _repo import REPO_ROOT

from studio import config, provider_quota


def _no_realtime(monkeypatch):
    """擋掉所有會打網路的即時額度查詢，讓 snapshot 純跑結構（hermetic）。"""
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
    monkeypatch.setattr(config, "claude_cli_logged_in", lambda: False)
    monkeypatch.setattr(config, "codex_cli_logged_in", lambda: False)
    monkeypatch.setattr(
        provider_quota.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": None},
    )


def test_only_four_providers_no_history(monkeypatch):
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: False)
    monkeypatch.setattr(config, "provider_ready", lambda: True)
    _no_realtime(monkeypatch)

    data = provider_quota.snapshot()

    # 只剩 4 個 provider，固定順序；openai / gemini 已移除
    assert [p["key"] for p in data["providers"]] == ["claude", "codex", "antigravity", "minimax"]
    # 不再回傳歷史累積用量
    assert "usage" not in data
    for p in data["providers"]:
        assert "usage_5h" not in p
        assert "usage_all" not in p
        # 每個卡片都帶 rate_limits 鍵（即時剩餘額度；未設定/未登入時為 None）
        assert "rate_limits" in p


def test_minimax_rate_limits_queried_when_key_set(monkeypatch):
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: False)
    monkeypatch.setattr(config, "provider_ready", lambda: True)
    monkeypatch.setattr(config, "claude_cli_logged_in", lambda: False)
    monkeypatch.setattr(config, "codex_cli_logged_in", lambda: False)
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "mm-key")
    sentinel = {
        "five_hour": {"used_percentage": 1.0, "reset_at": 1.0},
        "seven_day": None,
        "error": None,
    }
    monkeypatch.setattr(
        provider_quota.minimax_usage, "fetch_rate_limits", lambda *_a, **_k: sentinel
    )
    monkeypatch.setattr(
        provider_quota.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": None},
    )

    data = provider_quota.snapshot()
    mm = next(p for p in data["providers"] if p["key"] == "minimax")
    assert mm["rate_limits"] is sentinel


def _claude_multi_account(monkeypatch, accounts: list[dict], rate_limits: dict) -> list[str]:
    """佈置「claude 訂閱＋多帳號」情境；回傳呼叫順序記錄（sync/list）供斷言。"""
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: False)
    monkeypatch.setattr(config, "provider_ready", lambda: True)
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
    monkeypatch.setattr(config, "codex_cli_logged_in", lambda: False)
    monkeypatch.setattr(config, "claude_cli_logged_in", lambda: True)
    monkeypatch.setattr(config, "has_api_key", lambda: False)
    monkeypatch.setattr(
        provider_quota.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": None},
    )
    calls: list[str] = []
    monkeypatch.setattr(
        provider_quota.claude_accounts,
        "sync_active_label",
        lambda: calls.append("sync") or False,
    )
    monkeypatch.setattr(
        provider_quota.claude_accounts,
        "list_accounts",
        lambda: calls.append("list") or accounts,
    )
    monkeypatch.setattr(
        provider_quota.claude_usage, "fetch_rate_limits", lambda *_a, **_k: rate_limits
    )
    return calls


def test_claude_stale_label_mapping_and_sync_called(monkeypatch):
    """非在線帳號的 unauthorized → stale_label；在線帳號保留原 error；sync 先於 list。"""
    unauthorized = {
        "five_hour": None,
        "seven_day": None,
        "fetched_at": 0.0,
        "error": "unauthorized",
    }
    accounts = [
        {"label": "A", "cred_file": "/tmp/acct-A.json", "subscription": "max", "active": True},
        {"label": "B", "cred_file": "/tmp/acct-B.json", "subscription": "max", "active": False},
    ]
    calls = _claude_multi_account(monkeypatch, accounts, unauthorized)

    data = provider_quota.snapshot()

    claude = next(p for p in data["providers"] if p["key"] == "claude")
    by = {a["label"]: a for a in claude["accounts"]}
    assert by["A"]["rate_limits"]["error"] == "unauthorized"  # active：真過期就誠實顯示
    assert by["B"]["rate_limits"]["error"] == "stale_label"  # inactive：快照 stale，非登出
    # 不得就地改動 claude_usage 的 TTL 快取物件（映射須回新 dict）
    assert unauthorized["error"] == "unauthorized"
    # snapshot 讀帳號前先同步在線 label 快照
    assert calls[:2] == ["sync", "list"]


def test_claude_snapshot_survives_sync_failure(monkeypatch):
    """sync_active_label 炸掉不得拖垮 snapshot（包 try/except）。"""
    ok = {"five_hour": None, "seven_day": None, "fetched_at": 0.0, "error": None}
    accounts = [
        {"label": "A", "cred_file": "/tmp/acct-A.json", "subscription": "max", "active": True},
    ]
    _claude_multi_account(monkeypatch, accounts, ok)

    def _boom():
        raise RuntimeError("sync 壞掉")

    monkeypatch.setattr(provider_quota.claude_accounts, "sync_active_label", _boom)

    data = provider_quota.snapshot()

    assert data["ok"] is True
    claude = next(p for p in data["providers"] if p["key"] == "claude")
    assert [a["label"] for a in claude["accounts"]] == ["A"]
    assert claude["accounts"][0]["rate_limits"]["error"] is None  # error 不因 sync 失敗改變


def test_frontend_rl_errors_has_stale_label_message():
    """前後端契約：後端新 error 碼 stale_label 在前端 RL_ERRORS 有對應訊息。"""
    src = (REPO_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = src.index("const RL_ERRORS")
    end = src.index("};", start)
    table = src[start:end]
    assert "stale_label:" in table
    assert "切換到此帳號一次即會刷新" in table


def test_antigravity_signed_in(monkeypatch):
    """有效 token：直接附 rate_limits、ready/signed_in，且不再跑 `agy models` 子程序。"""
    monkeypatch.setattr(config, "PROVIDER", "antigravity")
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "/usr/local/bin/agy")
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: True)
    monkeypatch.setattr(config, "provider_ready", lambda: True)
    _no_realtime(monkeypatch)

    # 新設計結構性保證不再跑 `agy models` 子程序：provider_quota 模組根本不 import subprocess。

    data = provider_quota.snapshot()
    agy = next(p for p in data["providers"] if p["key"] == "antigravity")

    assert data["active_provider"] == "antigravity"
    assert agy["ready"] is True
    assert agy["auth"] == "signed_in"
    assert agy["status"] == "ok"
    assert agy["models"] == []  # 不再由子程序填充
    assert agy["rate_limits"] is not None and agy["rate_limits"]["error"] is None
    # 不洩漏任何 token 值
    assert "token" not in str(agy).lower()


def test_antigravity_needs_login(monkeypatch):
    """完全沒登入（token_missing）：ready False、needs_login，但仍附 rate_limits（含 error）。"""
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "agy")
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: True)
    monkeypatch.setattr(config, "provider_ready", lambda: False)
    _no_realtime(monkeypatch)
    monkeypatch.setattr(
        provider_quota.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": "token_missing"},
    )

    data = provider_quota.snapshot()
    agy = next(p for p in data["providers"] if p["key"] == "antigravity")

    assert agy["ready"] is False
    assert agy["status"] == "warn"
    assert agy["auth"] == "needs_login"
    assert "登入" in agy["quota"]["detail"]
    # 不再 None：附 error 讓前端顯示明確訊息而非空白
    assert agy["rate_limits"]["error"] == "token_missing"


def test_antigravity_token_expired(monkeypatch):
    """token 過期（unauthorized）：仍視為 ready（可由跑討論刷新），附 rate_limits.error。"""
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "agy")
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: True)
    monkeypatch.setattr(config, "provider_ready", lambda: False)
    _no_realtime(monkeypatch)
    monkeypatch.setattr(
        provider_quota.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": "unauthorized"},
    )

    data = provider_quota.snapshot()
    agy = next(p for p in data["providers"] if p["key"] == "antigravity")

    assert agy["ready"] is True  # 有 token，只是過期
    assert agy["auth"] == "signed_in"
    assert agy["status"] == "warn"
    assert "過期" in agy["quota"]["detail"]
    assert agy["rate_limits"]["error"] == "unauthorized"


# --- snapshot() 的 SWR（stale-while-revalidate）模組級快取 --------------------


def _swr_snap(tag: str, updated_at: float | None = None) -> dict:
    """極簡假快照（頂層結構同 ``_fetch()``；``tag`` 供斷言分辨新舊快照）。"""
    return {
        "ok": True,
        "active_provider": "claude",
        "provider_ready": True,
        "updated_at": time.time() if updated_at is None else updated_at,
        "stale": False,
        "providers": [],
        "tag": tag,
    }


def _seed_cache(monkeypatch, snap: dict, age: float) -> None:
    """把 ``snap`` 植入模組級快取，佈置成「``age`` 秒前查得」。"""
    monkeypatch.setattr(provider_quota, "_cache", (time.time() - age, snap))


def test_swr_fresh_cache_returns_without_fetch(monkeypatch):
    """快取新鮮（< TTL）：直接回快取，不觸發任何 fetch。"""
    fresh = _swr_snap("fresh")
    _seed_cache(monkeypatch, fresh, age=1.0)
    monkeypatch.setattr(provider_quota, "_fetch", lambda: pytest.fail("新鮮快取不得觸發 fetch"))

    data = provider_quota.snapshot()

    assert data is fresh
    assert data["stale"] is False
    assert provider_quota._refresh_thread is None


def test_swr_no_cache_fetches_synchronously(monkeypatch):
    """無快取（首次啟動）：同步查（現行為），並把結果填入快取供下次直回。"""
    calls: list[int] = []
    fresh = _swr_snap("v1")
    monkeypatch.setattr(provider_quota, "_fetch", lambda: calls.append(1) or fresh)

    assert provider_quota.snapshot() is fresh
    assert calls == [1]
    # 快取已填：60s 內再呼叫直接回、不再 fetch
    assert provider_quota.snapshot() is fresh
    assert calls == [1]


def test_swr_stale_returns_immediately_and_refreshes_in_background(monkeypatch):
    """過期但未超過 STALE_MAX：立即回 stale 舊快照，背景刷新完成後快取更新。

    同時以 time.perf_counter 量測：舊行為會同步卡在慢 fetch（此處以 Event 模擬「等最慢
    provider」），SWR 後 stale 呼叫毫秒級返回。
    """
    old = _swr_snap("old", updated_at=1000.0)
    monkeypatch.setattr(config, "QUOTA_STALE_MAX", 300.0)
    _seed_cache(monkeypatch, old, age=120.0)

    release = threading.Event()
    new = _swr_snap("new")

    def slow_fetch() -> dict:
        release.wait(timeout=10)  # 模擬慢 provider：舊行為呼叫端會同步卡在這裡
        return new

    monkeypatch.setattr(provider_quota, "_fetch", slow_fetch)

    t0 = time.perf_counter()
    data = provider_quota.snapshot()
    elapsed = time.perf_counter() - t0

    assert data["stale"] is True
    assert data["tag"] == "old"
    assert data["updated_at"] == 1000.0  # 既有欄位保留舊值，前端可據以顯示「更新中…」
    assert elapsed < 0.2  # 不等慢 fetch（本機 <1ms；門檻放寬防 CI 抖動）
    assert old["stale"] is False  # 回複本、不就地改動快取物件

    thread = provider_quota._refresh_thread
    assert thread is not None  # 已觸發一次背景刷新
    release.set()
    thread.join(timeout=10)
    assert provider_quota._cache[1] is new  # 背景刷新完成後快取已更新
    assert provider_quota.snapshot() is new  # 下次呼叫直接拿新快照（不再 stale）


def test_swr_single_flight_concurrent_calls_refresh_once(monkeypatch):
    """單飛：刷新進行中再遇 stale 呼叫（含並發）只回舊快照，不重複觸發刷新。"""
    old = _swr_snap("old")
    monkeypatch.setattr(config, "QUOTA_STALE_MAX", 300.0)
    _seed_cache(monkeypatch, old, age=120.0)

    release = threading.Event()
    calls: list[int] = []
    new = _swr_snap("new")

    def slow_fetch() -> dict:
        calls.append(1)
        release.wait(timeout=10)
        return new

    monkeypatch.setattr(provider_quota, "_fetch", slow_fetch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _i: provider_quota.snapshot(), range(2)))

    assert all(r["stale"] is True and r["tag"] == "old" for r in results)
    thread = provider_quota._refresh_thread
    assert thread is not None
    release.set()
    thread.join(timeout=10)
    assert calls == [1]  # 兩次並發只觸發一次刷新
    assert provider_quota._cache[1] is new


def test_swr_beyond_stale_max_fetches_synchronously(monkeypatch):
    """超過 STALE_MAX：太舊的快照不再回給呼叫端，改走同步查（現行為）。"""
    old = _swr_snap("old")
    monkeypatch.setattr(config, "QUOTA_STALE_MAX", 300.0)
    _seed_cache(monkeypatch, old, age=301.0)
    calls: list[int] = []
    new = _swr_snap("new")
    monkeypatch.setattr(provider_quota, "_fetch", lambda: calls.append(1) or new)

    data = provider_quota.snapshot()

    assert data is new
    assert data["stale"] is False
    assert calls == [1]
    assert provider_quota._refresh_thread is None  # 走同步路徑，未觸發背景刷新


def test_swr_disabled_when_stale_max_zero(monkeypatch):
    """QUOTA_STALE_MAX=0＝停用 SWR：快取一過期就同步查（回到舊行為）。"""
    monkeypatch.setattr(config, "QUOTA_STALE_MAX", 0.0)
    _seed_cache(monkeypatch, _swr_snap("old"), age=61.0)
    new = _swr_snap("new")
    monkeypatch.setattr(provider_quota, "_fetch", lambda: new)

    assert provider_quota.snapshot() is new
    assert provider_quota._refresh_thread is None


def test_swr_refresh_failure_keeps_stale_snapshot(monkeypatch, caplog):
    """背景刷新失敗：保留舊快照、記 log、不拋出；單飛 flag 釋放供之後重試。"""
    old = _swr_snap("old")
    monkeypatch.setattr(config, "QUOTA_STALE_MAX", 300.0)
    seeded = (time.time() - 120.0, old)
    monkeypatch.setattr(provider_quota, "_cache", seeded)

    def boom() -> dict:
        raise RuntimeError("上游額度端點壞掉")

    monkeypatch.setattr(provider_quota, "_fetch", boom)

    with caplog.at_level(logging.ERROR, logger="ti.provider_quota"):
        data = provider_quota.snapshot()
        thread = provider_quota._refresh_thread
        assert thread is not None
        thread.join(timeout=10)

    assert data["stale"] is True
    assert data["tag"] == "old"
    assert provider_quota._cache is seeded  # 刷新失敗保留舊快照
    assert provider_quota._refresh_thread is None  # 單飛 flag 已釋放，之後可再重試
    assert "背景刷新失敗" in caplog.text


def test_quota_stale_max_env_reload(monkeypatch):
    """TI_QUOTA_STALE_MAX 走 config.reload() 執行期生效；還原後回預設 300。"""
    monkeypatch.setenv("TI_QUOTA_STALE_MAX", "120")
    config.reload()
    try:
        assert config.QUOTA_STALE_MAX == 120.0
    finally:
        monkeypatch.delenv("TI_QUOTA_STALE_MAX")
        config.reload()
    assert config.QUOTA_STALE_MAX == 300.0


def test_frontend_renders_stale_hint():
    """前後端契約：settings 面板讀到 stale=true 時顯示「額度更新中」muted 提示。"""
    src = (REPO_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = src.index("function renderProviderQuota")
    end = src.index("\nfunction ", start + 1)
    body = src[start:end]
    assert "data.stale" in body
    assert "額度更新中" in body
