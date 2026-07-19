"""Telegram 推播 sink(第 3 階 A1):與 webhook 並存、各自獨立成敗、test 端點回報。

守護不變量:
- token+chat_id 皆非空才啟用;缺一=該 sink 不出現(零網路)。
- 純文字 sendMessage(無 parse_mode),內容帶 kind/title/extra;URL 內嵌 token,
  失敗 log 不含 URL。
- 雙 sink 並存:webhook 失敗不影響 telegram(反之亦然);send=任一送達 True。
- send_test:落檔 test 事件+回報各 sink 成敗;皆未設定 → ok=False、sinks={}。
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from studio import config, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    return tmp_path


def _capture(monkeypatch, *, fail_urls=()):
    calls: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if any(f in url for f in fail_urls):
            raise OSError("boom")
        calls.append({"url": url, "body": json.loads(req.data.decode("utf-8"))})
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def test_telegram_send_message_payload(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    calls = _capture(monkeypatch)
    assert notify.send("quota_exhausted", "額度耗盡", account="B") is True
    assert calls[0]["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    body = calls[0]["body"]
    assert body["chat_id"] == "42" and body["disable_web_page_preview"] is True
    assert "quota_exhausted" in body["text"] and "額度耗盡" in body["text"]
    assert "account=B" in body["text"]


def test_telegram_requires_both_token_and_chat_id(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")  # 缺 chat_id
    calls = _capture(monkeypatch)
    assert notify.send("x", "y") is False
    assert not calls, "缺 chat_id=sink 未啟用,零網路"


def test_dual_sink_independent_failure(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    calls = _capture(monkeypatch, fail_urls=("hook.example",))
    assert notify.send("loop_stall", "停滯") is True, "webhook 失敗、telegram 送達=True"
    assert [c["url"] for c in calls] == ["https://api.telegram.org/bot123:abc/sendMessage"]


def test_send_test_reports_sinks(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    _capture(monkeypatch, fail_urls=("telegram.org",))
    out = notify.send_test()
    assert out == {"ok": True, "sinks": {"webhook": True, "telegram": False}}
    assert [e["kind"] for e in notify.read_events(1)] == ["test"], "test 事件照樣落檔"


def test_send_test_nothing_configured(monkeypatch):
    calls = _capture(monkeypatch)
    assert notify.send_test() == {"ok": False, "sinks": {}}
    assert not calls
