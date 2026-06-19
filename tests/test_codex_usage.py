"""codex_usage：codex app-server rateLimits 的正規化、快取與錯誤態。

不實際 spawn codex app-server，全部 monkeypatch _read_rate_limits（解析層另以 _window 單測）。
"""

from __future__ import annotations

import pytest

from studio import codex_usage

# codex app-server 回的 rateLimits 代表性片段（取自實測）。
SAMPLE_RL = {
    "limitId": "codex",
    "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1781867504},
    "secondary": {"usedPercent": 62, "windowDurationMins": 10080, "resetsAt": 1782340673},
    "planType": "prolite",
}


@pytest.fixture(autouse=True)
def _isolate():
    codex_usage._cache = None
    yield
    codex_usage._cache = None


def test_success_normalizes(monkeypatch):
    monkeypatch.setattr(codex_usage, "_read_rate_limits", lambda: SAMPLE_RL)
    r = codex_usage.fetch_rate_limits()
    assert r["error"] is None
    assert r["five_hour"]["used_percentage"] == 0.0
    assert r["five_hour"]["reset_at"] == 1781867504.0
    assert r["seven_day"]["used_percentage"] == 62.0
    assert r["seven_day"]["reset_at"] == 1782340673.0


def test_ttl_cache_skips_second_call(monkeypatch):
    calls = []

    def fake_read():
        calls.append(1)
        return SAMPLE_RL

    monkeypatch.setattr(codex_usage, "_read_rate_limits", fake_read)
    first = codex_usage.fetch_rate_limits()
    second = codex_usage.fetch_rate_limits()
    assert second is first
    assert len(calls) == 1


def test_force_bypasses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_usage, "_read_rate_limits", lambda: calls.append(1) or SAMPLE_RL)
    codex_usage.fetch_rate_limits()
    codex_usage.fetch_rate_limits(force=True)
    assert len(calls) == 2


def test_no_response_is_unreachable(monkeypatch):
    monkeypatch.setattr(codex_usage, "_read_rate_limits", lambda: None)
    r = codex_usage.fetch_rate_limits()
    assert r["error"] == "unreachable"
    assert r["five_hour"] is None and r["seven_day"] is None


def test_non_dict_response_is_unreachable(monkeypatch):
    monkeypatch.setattr(codex_usage, "_read_rate_limits", lambda: "garbage")
    assert codex_usage.fetch_rate_limits()["error"] == "unreachable"


def test_window_handles_none_and_missing():
    assert codex_usage._window(None) is None
    assert codex_usage._window({}) == {"used_percentage": None, "reset_at": None}
    assert codex_usage._window({"usedPercent": 44, "resetsAt": 123})["used_percentage"] == 44.0


def test_partial_windows(monkeypatch):
    # 只有 primary、缺 secondary
    monkeypatch.setattr(
        codex_usage, "_read_rate_limits", lambda: {"primary": {"usedPercent": 5, "resetsAt": 9}}
    )
    r = codex_usage.fetch_rate_limits()
    assert r["five_hour"]["used_percentage"] == 5.0
    assert r["seven_day"] is None
    assert r["error"] is None
