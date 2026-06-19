"""Provider quota/status endpoint tests（只回即時剩餘額度，4 個 provider，無歷史用量）。"""

from __future__ import annotations

from studio import config, routes


def _no_realtime(monkeypatch):
    """擋掉所有會打網路的即時額度查詢，讓 snapshot 純跑結構（hermetic）。"""
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
    monkeypatch.setattr(config, "claude_cli_logged_in", lambda: False)
    monkeypatch.setattr(config, "codex_cli_logged_in", lambda: False)
    monkeypatch.setattr(
        routes.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": None},
    )


def test_only_four_providers_no_history(monkeypatch):
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: False)
    monkeypatch.setattr(config, "provider_ready", lambda: True)
    _no_realtime(monkeypatch)

    data = routes._provider_quota_snapshot()

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
    monkeypatch.setattr(routes.minimax_usage, "fetch_rate_limits", lambda *_a, **_k: sentinel)
    monkeypatch.setattr(
        routes.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": None},
    )

    data = routes._provider_quota_snapshot()
    mm = next(p for p in data["providers"] if p["key"] == "minimax")
    assert mm["rate_limits"] is sentinel


def test_antigravity_signed_in(monkeypatch):
    """有效 token：直接附 rate_limits、ready/signed_in，且不再跑 `agy models` 子程序。"""
    monkeypatch.setattr(config, "PROVIDER", "antigravity")
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "/usr/local/bin/agy")
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: True)
    monkeypatch.setattr(config, "provider_ready", lambda: True)
    _no_realtime(monkeypatch)

    def boom(*_a, **_k):  # 新設計不應再用子程序列模型
        raise AssertionError("不應呼叫 subprocess.run（已移除 agy models gate）")

    monkeypatch.setattr(routes.subprocess, "run", boom)

    data = routes._provider_quota_snapshot()
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
        routes.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": "token_missing"},
    )

    data = routes._provider_quota_snapshot()
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
        routes.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": "unauthorized"},
    )

    data = routes._provider_quota_snapshot()
    agy = next(p for p in data["providers"] if p["key"] == "antigravity")

    assert agy["ready"] is True  # 有 token，只是過期
    assert agy["auth"] == "signed_in"
    assert agy["status"] == "warn"
    assert "過期" in agy["quota"]["detail"]
    assert agy["rate_limits"]["error"] == "unauthorized"
