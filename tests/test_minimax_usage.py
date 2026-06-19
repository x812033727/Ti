"""minimax_usage：MiniMax token_plan/remains 的正規化、快取與錯誤態。

全部 monkeypatch httpx.get 與 API key，不打網路、不碰真金鑰。
"""

from __future__ import annotations

import httpx
import pytest

from studio import config, minimax_usage as m

# token_plan/remains 代表性回應（取自實測；interval=5h、weekly=7d）。
SAMPLE = {
    "model_remains": [
        {
            "model_name": "video",
            "current_interval_remaining_percent": 100,
            "current_weekly_remaining_percent": 100,
            "end_time": 1781913600000,
            "weekly_end_time": 1782086400000,
        },
        {
            "model_name": "general",
            "current_interval_remaining_percent": 99,
            "current_weekly_remaining_percent": 100,
            "end_time": 1781863200000,
            "weekly_end_time": 1782086400000,
        },
    ],
    "base_resp": {"status_code": 0, "status_msg": "success"},
}


class FakeResp:
    def __init__(self, status_code=200, body=None, raise_json=False):
        self.status_code = status_code
        self._body = body if body is not None else SAMPLE
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._body


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    m._cache = None
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "mm-key")
    monkeypatch.setattr(config, "MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    yield
    m._cache = None


def _patch_get(monkeypatch, resp_or_exc, counter=None):
    def fake_get(url, headers=None, timeout=None):
        if counter is not None:
            counter.append(url)
        assert url == "https://api.minimax.io/v1/token_plan/remains"
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        assert headers["Authorization"] == "Bearer mm-key"
        return resp_or_exc

    monkeypatch.setattr(m.httpx, "get", fake_get)


def test_success_picks_general_model(monkeypatch):
    _patch_get(monkeypatch, FakeResp())
    r = m.fetch_rate_limits()
    assert r["error"] is None
    # 取 general（非第一筆 video）：interval 99% remaining → 1% used
    assert r["five_hour"]["used_percentage"] == 1.0
    assert r["five_hour"]["reset_at"] == 1781863200.0  # ms → s
    assert r["seven_day"]["used_percentage"] == 0.0
    assert r["seven_day"]["reset_at"] == 1782086400.0


def test_fallback_first_model_when_no_general(monkeypatch):
    body = {
        "model_remains": [
            {
                "model_name": "video",
                "current_interval_remaining_percent": 80,
                "current_weekly_remaining_percent": 50,
                "end_time": 1000,
                "weekly_end_time": 2000,
            }
        ],
        "base_resp": {"status_code": 0},
    }
    _patch_get(monkeypatch, FakeResp(body=body))
    r = m.fetch_rate_limits()
    assert r["five_hour"]["used_percentage"] == 20.0
    assert r["seven_day"]["used_percentage"] == 50.0


def test_ttl_cache(monkeypatch):
    calls = []
    _patch_get(monkeypatch, FakeResp(), counter=calls)
    first = m.fetch_rate_limits()
    second = m.fetch_rate_limits()
    assert second is first
    assert len(calls) == 1


def test_force_bypasses_cache(monkeypatch):
    calls = []
    _patch_get(monkeypatch, FakeResp(), counter=calls)
    m.fetch_rate_limits()
    m.fetch_rate_limits(force=True)
    assert len(calls) == 2


def test_token_missing(monkeypatch):
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
    _patch_get(monkeypatch, AssertionError("should not call upstream"))
    r = m.fetch_rate_limits()
    assert r["error"] == "token_missing"
    assert r["five_hour"] is None


def test_http_401_unauthorized(monkeypatch):
    _patch_get(monkeypatch, FakeResp(status_code=401))
    assert m.fetch_rate_limits()["error"] == "unauthorized"


def test_base_resp_auth_error_unauthorized(monkeypatch):
    _patch_get(
        monkeypatch, FakeResp(body={"base_resp": {"status_code": 1004, "status_msg": "auth"}})
    )
    assert m.fetch_rate_limits()["error"] == "unauthorized"


def test_base_resp_other_error_unreachable(monkeypatch):
    _patch_get(monkeypatch, FakeResp(body={"base_resp": {"status_code": 2013, "status_msg": "x"}}))
    assert m.fetch_rate_limits()["error"] == "unreachable"


def test_network_error_unreachable(monkeypatch):
    _patch_get(monkeypatch, httpx.HTTPError("boom"))
    assert m.fetch_rate_limits()["error"] == "unreachable"


def test_empty_models_unreachable(monkeypatch):
    _patch_get(monkeypatch, FakeResp(body={"model_remains": [], "base_resp": {"status_code": 0}}))
    assert m.fetch_rate_limits()["error"] == "unreachable"
