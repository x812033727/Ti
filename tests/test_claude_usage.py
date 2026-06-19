"""claude_usage：Anthropic OAuth usage 端點查詢的正規化、快取與錯誤態。

純 HTTP/IO，全部 monkeypatch httpx.get 與憑證路徑，不打網路、不碰真 token。
"""

from __future__ import annotations

import json

import httpx
import pytest

from studio import claude_usage, config

# 端點真實回應的代表性片段（取自實測，含 model-scoped 視窗與 null）。
SAMPLE = {
    "five_hour": {"utilization": 3.0, "resets_at": "2026-06-19T05:49:59.372939+00:00"},
    "seven_day": {"utilization": 93.0, "resets_at": "2026-06-19T22:59:59.372961+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 44.0, "resets_at": "2026-06-19T23:00:00.372969+00:00"},
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
def _isolate(monkeypatch, tmp_path):
    """每測試清空模組快取，並把憑證指向 tmp（預設寫入一份有效 token）。"""
    claude_usage._cache = None
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-abc"}}), encoding="utf-8")
    monkeypatch.setattr(config, "CLAUDE_CREDENTIALS_FILE", cred)
    yield
    claude_usage._cache = None


def _patch_get(monkeypatch, resp_or_exc, counter=None):
    def fake_get(url, headers=None, timeout=None):
        if counter is not None:
            counter.append(url)
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        # 帶上正確的 auth + beta header 才算過
        assert headers["Authorization"] == "Bearer tok-abc"
        assert headers["anthropic-beta"] == "oauth-2025-04-20"
        return resp_or_exc

    monkeypatch.setattr(claude_usage.httpx, "get", fake_get)


def test_success_normalizes(monkeypatch):
    _patch_get(monkeypatch, FakeResp())
    r = claude_usage.fetch_rate_limits()
    assert r["error"] is None
    assert r["five_hour"]["used_percentage"] == 3.0
    assert r["seven_day"]["used_percentage"] == 93.0
    assert r["seven_day_sonnet"]["used_percentage"] == 44.0
    assert r["seven_day_opus"] is None
    # ISO → epoch（2026-06-19T05:49:59Z 應落在合理範圍且為 float）
    assert isinstance(r["five_hour"]["reset_at"], float)
    assert r["five_hour"]["reset_at"] > 1.7e9


def test_ttl_cache_skips_second_call(monkeypatch):
    calls = []
    _patch_get(monkeypatch, FakeResp(), counter=calls)
    first = claude_usage.fetch_rate_limits()
    second = claude_usage.fetch_rate_limits()
    assert second is first  # 同一物件＝命中快取
    assert len(calls) == 1  # 只打上游一次


def test_force_bypasses_cache(monkeypatch):
    calls = []
    _patch_get(monkeypatch, FakeResp(), counter=calls)
    claude_usage.fetch_rate_limits()
    claude_usage.fetch_rate_limits(force=True)
    assert len(calls) == 2


def test_token_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CLAUDE_CREDENTIALS_FILE", tmp_path / "nope.json")
    # 不該打 httpx
    _patch_get(monkeypatch, AssertionError("should not call upstream"))
    r = claude_usage.fetch_rate_limits()
    assert r["error"] == "token_missing"
    assert r["five_hour"] is None


def test_unauthorized(monkeypatch):
    _patch_get(monkeypatch, FakeResp(status_code=401))
    r = claude_usage.fetch_rate_limits()
    assert r["error"] == "unauthorized"


def test_server_error_is_unreachable(monkeypatch):
    _patch_get(monkeypatch, FakeResp(status_code=500))
    assert claude_usage.fetch_rate_limits()["error"] == "unreachable"


def test_network_error_is_unreachable(monkeypatch):
    _patch_get(monkeypatch, httpx.HTTPError("boom"))
    assert claude_usage.fetch_rate_limits()["error"] == "unreachable"


def test_bad_json_is_unreachable(monkeypatch):
    _patch_get(monkeypatch, FakeResp(raise_json=True))
    assert claude_usage.fetch_rate_limits()["error"] == "unreachable"


@pytest.mark.parametrize(
    "s,ok",
    [
        ("2026-06-19T05:49:59.372939+00:00", True),
        ("2026-06-19T05:49:59+00:00", True),
        ("not-a-date", False),
        ("", False),
        (None, False),
        (123, False),
    ],
)
def test_iso_to_epoch(s, ok):
    out = claude_usage._iso_to_epoch(s)
    assert (out is not None) == ok
    if ok:
        assert isinstance(out, float)


def test_window_handles_none_and_missing_util():
    assert claude_usage._window(None) is None
    assert (
        claude_usage._window({"resets_at": "2026-06-19T05:49:59+00:00"})["used_percentage"] is None
    )
