"""Provider quota/status endpoint tests."""

from __future__ import annotations

from types import SimpleNamespace

from studio import config, routes


def _agg():
    return {
        "sessions": 1,
        "total": {"prompt": 10, "completion": 5, "total": 15, "cost_usd": 0.0, "calls": 2},
        "by_provider": {
            "antigravity": {
                "prompt": 10,
                "completion": 5,
                "total": 15,
                "cost_usd": 0.0,
                "calls": 2,
            }
        },
        "by_model": {},
        "by_role": {},
        "est_extra_usd": 0.0,
    }


def test_provider_quota_snapshot_antigravity_models(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "antigravity")
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "/usr/local/bin/agy")
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: True)
    monkeypatch.setattr(config, "provider_ready", lambda: True)
    monkeypatch.setattr(routes.usage_report, "aggregate", lambda *_args, **_kwargs: _agg())
    # signed_in 時會查 Google Code Assist 配額——隔離掉，避免打網路且不依賴 token 檔。
    monkeypatch.setattr(
        routes.antigravity_usage,
        "fetch_rate_limits",
        lambda *_a, **_k: {"buckets": [], "fetched_at": 0.0, "error": None},
    )

    def fake_run(argv, **_kwargs):
        assert argv == ["/usr/local/bin/agy", "models"]
        return SimpleNamespace(
            returncode=0,
            stdout="Gemini 3.5 Flash (Medium)\nClaude Sonnet 4.6 (Thinking)\n",
            stderr="",
        )

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    data = routes._provider_quota_snapshot()
    agy = next(p for p in data["providers"] if p["key"] == "antigravity")

    assert data["active_provider"] == "antigravity"
    assert agy["ready"] is True
    assert agy["auth"] == "signed_in"
    assert agy["models"] == ["Gemini 3.5 Flash (Medium)", "Claude Sonnet 4.6 (Thinking)"]
    assert data["usage"]["last_5h"]["total"]["total"] == 15
    assert agy["usage_5h"]["total"] == 15
    assert agy["usage_7d"]["total"] == 15
    assert agy["usage_30d"]["total"] == 15
    assert "token" not in str(agy).lower()


def test_provider_quota_snapshot_antigravity_needs_login(monkeypatch):
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", "agy")
    monkeypatch.setattr(config, "antigravity_cli_available", lambda: True)
    monkeypatch.setattr(config, "provider_ready", lambda: False)
    monkeypatch.setattr(routes.usage_report, "aggregate", lambda *_args, **_kwargs: _agg())
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Please sign in to view available models.",
        ),
    )

    data = routes._provider_quota_snapshot()
    agy = next(p for p in data["providers"] if p["key"] == "antigravity")

    assert agy["ready"] is False
    assert agy["status"] == "warn"
    assert agy["auth"] == "needs_login"
    assert "Please sign in" in agy["quota"]["detail"]
