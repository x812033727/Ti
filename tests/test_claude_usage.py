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
    """每測試清空模組快取（TTL 快取＋last-known-good），並把憑證指向 tmp（預設寫入一份有效 token）。"""
    claude_usage._cache.clear()
    claude_usage._last_good.clear()
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-abc"}}), encoding="utf-8")
    monkeypatch.setattr(config, "CLAUDE_CREDENTIALS_FILE", cred)
    yield
    claude_usage._cache.clear()
    claude_usage._last_good.clear()


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


def test_cred_file_overrides_global(monkeypatch, tmp_path):
    """指定 cred_file 查另一帳號的標籤檔，token 取自該檔而非全域線上憑證。"""
    acct_b = tmp_path / ".credentials.acct-B.json"
    acct_b.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-B"}}), encoding="utf-8")
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["auth"] = headers["Authorization"]
        return FakeResp()

    monkeypatch.setattr(claude_usage.httpx, "get", fake_get)
    r = claude_usage.fetch_rate_limits(cred_file=acct_b)
    assert r["error"] is None
    assert seen["auth"] == "Bearer tok-B"  # 用了 acct-B 的 token，非全域 tok-abc


def test_cache_is_per_path(monkeypatch, tmp_path):
    """不同憑證檔各自獨立快取，互不命中（多帳號顯示的前提）。"""
    acct_b = tmp_path / ".credentials.acct-B.json"
    acct_b.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-B"}}), encoding="utf-8")
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(headers["Authorization"])
        return FakeResp()

    monkeypatch.setattr(claude_usage.httpx, "get", fake_get)
    claude_usage.fetch_rate_limits()  # 全域 tok-abc → 打上游
    claude_usage.fetch_rate_limits(cred_file=acct_b)  # acct-B tok-B → 打上游
    claude_usage.fetch_rate_limits()  # 命中全域快取
    claude_usage.fetch_rate_limits(cred_file=acct_b)  # 命中 acct-B 快取
    assert calls == ["Bearer tok-abc", "Bearer tok-B"]  # 各只打一次


def test_window_handles_none_and_missing_util():
    assert claude_usage._window(None) is None
    assert (
        claude_usage._window({"resets_at": "2026-06-19T05:49:59+00:00"})["used_percentage"] is None
    )


# --- 暫時性失敗的 last-known-good 回退（429 不得誤殺帳號）---------------------


def _fetch_success_then(monkeypatch, resp_or_exc):
    """先成功一次（填 last-known-good），再把上游換成指定失敗，並繞過 TTL 快取重查。"""
    _patch_get(monkeypatch, FakeResp())
    good = claude_usage.fetch_rate_limits()
    assert good["error"] is None
    _patch_get(monkeypatch, resp_or_exc)
    return good, claude_usage.fetch_rate_limits(force=True)


def test_429_falls_back_to_last_good_as_stale(monkeypatch):
    """成功後遇 429（實案：10:44 usage 端點 Too Many Requests）→ 回舊值副本：
    stale=True、error 維持 None、保留原 fetched_at——帳號輪替不得因此判帳號不可用。"""
    good, r = _fetch_success_then(monkeypatch, FakeResp(status_code=429))
    assert r["error"] is None
    assert r["stale"] is True
    assert r["fetched_at"] == good["fetched_at"]  # 保留舊快照的取得時間
    assert r["five_hour"]["used_percentage"] == 3.0  # 額度數字沿用舊值
    assert "stale" not in good  # 原成功快照不受污染（回退是副本）


def test_429_stale_copy_is_cached_for_ttl(monkeypatch):
    """回退結果照常進 TTL 快取：後續 60s 內的消費端看到同一份 stale 快照、不再打上游。"""
    _, stale = _fetch_success_then(monkeypatch, FakeResp(status_code=429))
    _patch_get(monkeypatch, AssertionError("TTL 內不得再打上游"))
    assert claude_usage.fetch_rate_limits() is stale


@pytest.mark.parametrize(
    "failure",
    [FakeResp(status_code=500), FakeResp(raise_json=True), httpx.HTTPError("boom")],
)
def test_transient_failures_fall_back_to_last_good(monkeypatch, failure):
    """5xx／壞 JSON／連線錯誤同樣回退舊值（與 429 同類的暫時性失敗）。"""
    _, r = _fetch_success_then(monkeypatch, failure)
    assert r["error"] is None and r["stale"] is True


def test_429_without_last_good_is_unreachable(monkeypatch):
    """從未成功過（無 last-known-good）→ 維持既有 unreachable 行為。"""
    _patch_get(monkeypatch, FakeResp(status_code=429))
    assert claude_usage.fetch_rate_limits()["error"] == "unreachable"


def test_401_not_masked_by_last_good(monkeypatch):
    """授權失敗（401）不得被舊值掩蓋：token 壞了就該回 unauthorized。"""
    _, r = _fetch_success_then(monkeypatch, FakeResp(status_code=401))
    assert r["error"] == "unauthorized"
    assert "stale" not in r


def test_last_good_older_than_max_age_is_unreachable(monkeypatch):
    """last-known-good 超過 900s → 不回退（過舊的額度資訊已不可信）。"""
    t0 = claude_usage._now()
    monkeypatch.setattr(claude_usage, "_now", lambda: t0)
    _patch_get(monkeypatch, FakeResp())
    assert claude_usage.fetch_rate_limits()["error"] is None
    # 時間快轉 901s：TTL 快取也已過期，重查遇 429 → 舊值過齡，回 unreachable
    monkeypatch.setattr(claude_usage, "_now", lambda: t0 + claude_usage._LAST_GOOD_MAX_AGE + 1)
    _patch_get(monkeypatch, FakeResp(status_code=429))
    assert claude_usage.fetch_rate_limits()["error"] == "unreachable"
