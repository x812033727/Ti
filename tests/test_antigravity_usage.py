"""antigravity_usage：查詢（loadCodeAssist 取 project → retrieveUserQuotaSummary 帶 project 取
Weekly/5h 群組 buckets，summary 失敗才退回每模型 retrieveUserQuota，再不行 fallback 層級）的
正規化、快取與錯誤態。

全部 monkeypatch httpx.post 與 token 路徑，不打網路、不碰真 token。依 URL 分派 response。
"""

from __future__ import annotations

import json

import httpx
import pytest

from studio import antigravity_usage as a, config

# retrieveUserQuotaSummary 回應：每 group 含 Weekly + 5h 兩個 bucket（agy /usage 同源）。
SUMMARY_SAMPLE = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "description": "Models within this group: Gemini Flash, Gemini Pro",
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "displayName": "Weekly Limit",
                    "window": "weekly",
                    "resetTime": "2026-06-26T18:35:04Z",
                    "remainingFraction": 0.92,
                },
                {
                    "bucketId": "gemini-5h",
                    "displayName": "Five Hour Limit",
                    "window": "5h",
                    "resetTime": "2026-06-20T05:44:17Z",
                    "remainingFraction": 0.4,
                },
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {"window": "weekly", "resetTime": "2026-06-27T05:22:09Z", "remainingFraction": 1},
                {"window": "5h", "resetTime": "2026-06-20T10:22:09Z", "remainingFraction": 1},
            ],
        },
    ]
}

# retrieveUserQuota 回應（每模型每日 REQUESTS）—— summary 不可用時的 fallback。
QUOTA_SAMPLE = {
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

TIER_SAMPLE = {
    "cloudaicompanionProject": "single-calling-cww5t",
    "currentTier": {
        "id": "standard-tier",
        "name": "Gemini Code Assist",
        "description": "Unlimited coding assistant with the most powerful Gemini models",
    },
    "paidTier": {
        "id": "g1-pro-tier",
        "name": "Gemini Code Assist in Google One AI Pro",
    },
}


class FakeResp:
    def __init__(self, status_code=200, body=None, raise_json=False):
        self.status_code = status_code
        self._body = body
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


def _patch(monkeypatch, *, tier=None, summary=None, quota=None, calls=None):
    """tier/summary/quota 各為 FakeResp 或 Exception；依 URL 分派；calls 記 (url, body)。"""

    by_url = {a.TIER_URL: tier, a.SUMMARY_URL: summary, a.QUOTA_URL: quota}

    def fake_post(url, headers=None, json=None, timeout=None):
        if calls is not None:
            calls.append((url, json))
        assert headers["Authorization"] == "Bearer tok-xyz"
        # 私有 API 以 UA 把關——所有請求都必須帶。
        assert headers["User-Agent"] == a._UA
        target = by_url.get(url)
        if isinstance(target, Exception):
            raise target
        if target is None:
            raise AssertionError(f"unexpected call to {url}")
        return target

    monkeypatch.setattr(a.httpx, "post", fake_post)


def test_buckets_from_summary(monkeypatch):
    # loadCodeAssist 給 project → retrieveUserQuotaSummary 帶 project → Weekly/5h 群組 buckets
    calls = []
    _patch(
        monkeypatch,
        tier=FakeResp(body=TIER_SAMPLE),
        summary=FakeResp(body=SUMMARY_SAMPLE),
        calls=calls,
    )
    r = a.fetch_rate_limits()
    assert r["error"] is None
    assert len(r["buckets"]) == 4  # 2 groups × {weekly, 5h}
    assert r["buckets"][0]["label"] == "Gemini · 7 天"
    assert r["buckets"][0]["used_percentage"] == 8.0  # 1 - 0.92
    assert r["buckets"][1]["label"] == "Gemini · 5 小時"
    assert r["buckets"][1]["used_percentage"] == 60.0  # 1 - 0.4
    assert r["buckets"][2]["label"] == "Claude and GPT · 7 天"
    assert r["buckets"][2]["used_percentage"] == 0.0
    assert isinstance(r["buckets"][0]["reset_at"], float)
    assert r["tier"]["tier_id"] == "standard-tier"  # tier 仍附帶
    # 驗證兩步＋summary 帶了正確 project（不打每模型 quota）
    assert calls[0][0] == a.TIER_URL
    assert calls[1] == (a.SUMMARY_URL, {"project": "single-calling-cww5t"})
    assert len(calls) == 2


def test_summary_403_falls_back_to_per_model_quota(monkeypatch):
    # summary 不可用（403）→ 退回每模型 retrieveUserQuota
    calls = []
    _patch(
        monkeypatch,
        tier=FakeResp(body=TIER_SAMPLE),
        summary=FakeResp(status_code=403),
        quota=FakeResp(body=QUOTA_SAMPLE),
        calls=calls,
    )
    r = a.fetch_rate_limits()
    assert r["error"] is None
    assert len(r["buckets"]) == 2  # TOKENS 濾掉，依用量降序
    assert r["buckets"][0]["label"] == "Gemini 2.5 Flash"
    assert r["buckets"][0]["used_percentage"] == 60.0
    assert [c[0] for c in calls] == [a.TIER_URL, a.SUMMARY_URL, a.QUOTA_URL]


def test_summary_empty_falls_back_to_per_model_quota(monkeypatch):
    # summary 200 但無群組 → 退回每模型 quota
    _patch(
        monkeypatch,
        tier=FakeResp(body=TIER_SAMPLE),
        summary=FakeResp(body={"groups": []}),
        quota=FakeResp(body=QUOTA_SAMPLE),
    )
    r = a.fetch_rate_limits()
    assert r["error"] is None
    assert len(r["buckets"]) == 2


def test_both_quota_403_falls_back_to_tier(monkeypatch):
    # summary 與 per-model 都 403（無數值配額）→ 顯示層級
    _patch(
        monkeypatch,
        tier=FakeResp(body=TIER_SAMPLE),
        summary=FakeResp(status_code=403),
        quota=FakeResp(status_code=403),
    )
    r = a.fetch_rate_limits()
    assert r["error"] is None
    assert r["buckets"] == []
    assert r["tier"]["label"] == "Gemini Code Assist"
    assert r["tier"]["unlimited"] is True
    assert r["tier"]["paid_tier"] == "Gemini Code Assist in Google One AI Pro"


def test_no_project_only_tier(monkeypatch):
    # loadCodeAssist 沒給 project → 不打 quota，只回層級
    body = {"currentTier": {"id": "free-tier", "name": "Free", "description": "Limited"}}
    calls = []
    _patch(monkeypatch, tier=FakeResp(body=body), calls=calls)
    r = a.fetch_rate_limits()
    assert r["error"] is None
    assert r["tier"]["unlimited"] is False
    assert r["tier"]["paid_tier"] is None
    assert [c[0] for c in calls] == [a.TIER_URL]  # 沒打 summary/quota


def test_loadcodeassist_401_unauthorized(monkeypatch):
    _patch(monkeypatch, tier=FakeResp(status_code=401))
    assert a.fetch_rate_limits()["error"] == "unauthorized"


def test_summary_401_unauthorized(monkeypatch):
    # loadCodeAssist OK 但 summary 401（token 中途失效）→ unauthorized
    _patch(monkeypatch, tier=FakeResp(body=TIER_SAMPLE), summary=FakeResp(status_code=401))
    assert a.fetch_rate_limits()["error"] == "unauthorized"


def test_per_model_quota_401_unauthorized(monkeypatch):
    # summary 403 退回 per-model，per-model 401 → unauthorized
    _patch(
        monkeypatch,
        tier=FakeResp(body=TIER_SAMPLE),
        summary=FakeResp(status_code=403),
        quota=FakeResp(status_code=401),
    )
    assert a.fetch_rate_limits()["error"] == "unauthorized"


def test_token_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ANTIGRAVITY_OAUTH_TOKEN_FILE", tmp_path / "nope")
    monkeypatch.setattr(a.httpx, "post", lambda *_a, **_k: pytest.fail("should not call upstream"))
    r = a.fetch_rate_limits()
    assert r["error"] == "token_missing"
    assert r["buckets"] == []
    assert r["tier"] is None


def test_loadcodeassist_server_error_unreachable(monkeypatch):
    _patch(monkeypatch, tier=FakeResp(status_code=500))
    assert a.fetch_rate_limits()["error"] == "unreachable"


def test_loadcodeassist_network_error_unreachable(monkeypatch):
    _patch(monkeypatch, tier=httpx.HTTPError("boom"))
    assert a.fetch_rate_limits()["error"] == "unreachable"


def test_loadcodeassist_bad_json_unreachable(monkeypatch):
    _patch(monkeypatch, tier=FakeResp(raise_json=True))
    assert a.fetch_rate_limits()["error"] == "unreachable"


def test_ttl_cache(monkeypatch):
    calls = []
    _patch(
        monkeypatch,
        tier=FakeResp(body=TIER_SAMPLE),
        summary=FakeResp(body=SUMMARY_SAMPLE),
        calls=calls,
    )
    first = a.fetch_rate_limits()
    second = a.fetch_rate_limits()
    assert second is first
    assert len(calls) == 2  # 一次完整查詢＝loadCodeAssist + summary


def test_force_bypasses_cache(monkeypatch):
    calls = []
    _patch(
        monkeypatch,
        tier=FakeResp(body=TIER_SAMPLE),
        summary=FakeResp(body=SUMMARY_SAMPLE),
        calls=calls,
    )
    a.fetch_rate_limits()
    a.fetch_rate_limits(force=True)
    assert len(calls) == 4  # 兩次完整查詢 × 兩步


def test_parse_summary_group_label():
    assert a._group_label("Gemini Models") == "Gemini"
    assert a._group_label("Claude and GPT models") == "Claude and GPT"
    assert a._group_label("") == "Antigravity"


def test_prettify():
    assert a._prettify("gemini-2.5-pro") == "Gemini 2.5 Pro"
    assert a._prettify("gemini-3.1-flash-lite") == "Gemini 3.1 Flash Lite"


@pytest.mark.parametrize(
    "s,ok",
    [
        ("2026-06-20T06:27:20Z", True),
        ("2026-06-19T14:33:23.970753744+08:00", True),  # 9 位奈秒
        ("bad", False),
        ("", False),
        (None, False),
    ],
)
def test_iso_to_epoch(s, ok):
    out = a._iso_to_epoch(s)
    assert (out is not None) == ok
