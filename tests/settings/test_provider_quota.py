"""Provider quota/status endpoint tests（只回即時剩餘額度，4 個 provider，無歷史用量）。"""

from __future__ import annotations

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
