"""antigravity_usage：Google Code Assist retrieveUserQuota 的正規化、快取與錯誤態。

全部 monkeypatch httpx.post 與 token 路徑，不打網路、不碰真 token。
"""

from __future__ import annotations

import json

import httpx
import pytest

from studio import antigravity_usage as a, config

SAMPLE = {
    "buckets": [
        {
            "resetTime": "2026-06-20T06:27:20Z",
            "tokenType": "REQUESTS",
            "modelId": "gemini-2.5-pro",
            "remainingFraction": 1,
        },
        {
            "resetTime": "2026-06-20T06:27:20Z",
            "tokenType": "REQUESTS",
            "modelId": "gemini-2.5-flash",
            "remainingFraction": 0.4,
        },
        # 非 REQUESTS 型應被濾掉
        {
            "resetTime": "2026-06-20T06:27:20Z",
            "tokenType": "TOKENS",
            "modelId": "gemini-2.5-pro",
            "remainingFraction": 0.1,
        },
    ]
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
    a._cache = None
    tokf = tmp_path / "antigravity-oauth-token"
    tokf.write_text(json.dumps({"token": {"access_token": "tok-xyz"}}), encoding="utf-8")
    monkeypatch.setattr(config, "ANTIGRAVITY_OAUTH_TOKEN_FILE", tokf)
    yield
    a._cache = None


def _patch_post(monkeypatch, resp_or_exc, counter=None):
    def fake_post(url, headers=None, json=None, timeout=None):
        if counter is not None:
            counter.append(url)
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        assert headers["Authorization"] == "Bearer tok-xyz"
        return resp_or_exc

    monkeypatch.setattr(a.httpx, "post", fake_post)


def test_success_normalizes_and_sorts(monkeypatch):
    _patch_post(monkeypatch, FakeResp())
    r = a.fetch_rate_limits()
    assert r["error"] is None
    # TOKENS 型被濾掉 → 只剩 2 個 REQUESTS bucket
    assert len(r["buckets"]) == 2
    # 依 used_percentage 降序：flash(used 60%) 在前、pro(0%) 在後
    assert r["buckets"][0]["label"] == "Gemini 2.5 Flash"
    assert r["buckets"][0]["used_percentage"] == 60.0
    assert r["buckets"][1]["label"] == "Gemini 2.5 Pro"
    assert r["buckets"][1]["used_percentage"] == 0.0
    assert isinstance(r["buckets"][0]["reset_at"], float)


def test_ttl_cache(monkeypatch):
    calls = []
    _patch_post(monkeypatch, FakeResp(), counter=calls)
    first = a.fetch_rate_limits()
    second = a.fetch_rate_limits()
    assert second is first
    assert len(calls) == 1


def test_force_bypasses_cache(monkeypatch):
    calls = []
    _patch_post(monkeypatch, FakeResp(), counter=calls)
    a.fetch_rate_limits()
    a.fetch_rate_limits(force=True)
    assert len(calls) == 2


def test_token_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ANTIGRAVITY_OAUTH_TOKEN_FILE", tmp_path / "nope")
    _patch_post(monkeypatch, AssertionError("should not call upstream"))
    r = a.fetch_rate_limits()
    assert r["error"] == "token_missing"
    assert r["buckets"] == []


def test_unauthorized(monkeypatch):
    _patch_post(monkeypatch, FakeResp(status_code=403))
    assert a.fetch_rate_limits()["error"] == "unauthorized"


def test_server_error_unreachable(monkeypatch):
    _patch_post(monkeypatch, FakeResp(status_code=500))
    assert a.fetch_rate_limits()["error"] == "unreachable"


def test_network_error_unreachable(monkeypatch):
    _patch_post(monkeypatch, httpx.HTTPError("boom"))
    assert a.fetch_rate_limits()["error"] == "unreachable"


def test_bad_json_unreachable(monkeypatch):
    _patch_post(monkeypatch, FakeResp(raise_json=True))
    assert a.fetch_rate_limits()["error"] == "unreachable"


def test_prettify():
    assert a._prettify("gemini-2.5-pro") == "Gemini 2.5 Pro"
    assert a._prettify("gemini-3.1-flash-lite") == "Gemini 3.1 Flash Lite"


@pytest.mark.parametrize(
    "s,ok",
    [
        ("2026-06-20T06:27:20Z", True),
        ("2026-06-19T14:33:23.970753744+08:00", True),  # 9 位奈秒
        ("2026-06-19T14:33:23.970753+08:00", True),
        ("bad", False),
        ("", False),
        (None, False),
    ],
)
def test_iso_to_epoch_nanoseconds(s, ok):
    out = a._iso_to_epoch(s)
    assert (out is not None) == ok
    if ok:
        assert isinstance(out, float)
