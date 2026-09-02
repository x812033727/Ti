"""QA 破壞性驗證：通知 webhook / sink 邊界情境。"""

from __future__ import annotations

import json
import logging

from studio import config, notify


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _InlineThread:
    instances: list[_InlineThread] = []

    def __init__(self, target, args=(), kwargs=None, daemon=False, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = {} if kwargs is None else kwargs
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True
        self.target(*self.args, **self.kwargs)


def test_payload_and_delivery_evidence_do_not_store_webhook_secret(tmp_path, monkeypatch):
    webhook = "https://hooks.example.test/notify?token=secret-token"
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.get_full_url(),
                "timeout": timeout,
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        return _Response()

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", webhook)
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 2.25)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    ok = notify.send(
        "task_failed",
        f"失敗端點 {webhook}",
        webhook_url=webhook,
        api_token="secret-token",
        trace_path="/tmp/private/log.txt",
    )

    assert ok is True
    assert calls[0]["timeout"] == 2.25
    body_dump = json.dumps(calls[0]["body"], ensure_ascii=False)
    assert webhook not in body_dump
    assert "secret-token" not in body_dump
    assert calls[0]["body"]["title"] == "失敗端點 ***"
    assert calls[0]["body"]["webhook_url"] == {"configured": True}
    assert calls[0]["body"]["api_token"] == {"configured": True}
    assert calls[0]["body"]["trace_path"] == "[redacted-path]"

    deliveries = notify.read_deliveries(1)
    assert len(deliveries) == 1
    assert deliveries[0]["alert_kind"] == "task_failed"
    assert deliveries[0]["sink"] == "webhook"
    assert deliveries[0]["ok"] is True
    assert deliveries[0]["error"] == ""
    assert deliveries[0]["drill"] is False
    assert webhook not in json.dumps(deliveries, ensure_ascii=False)
    assert "secret-token" not in json.dumps(deliveries, ensure_ascii=False)


def test_webhook_failure_log_redacts_url_even_if_transport_error_mentions_it(
    tmp_path, monkeypatch, caplog
):
    webhook = "https://hooks.example.test/notify?token=transport-secret"

    def fake_urlopen(request, timeout=None):
        raise OSError(f"transport failed for {request.get_full_url()}")

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", webhook)
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 0.5)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    caplog.set_level(logging.DEBUG, logger="ti.notify")

    assert notify.send("task_failed", f"通知失敗 {webhook}", webhook_url=webhook) is False

    assert webhook not in caplog.text
    assert "transport-secret" not in caplog.text


def test_send_bg_with_only_telegram_sink_still_starts_daemon_and_uses_timeout(
    tmp_path, monkeypatch
):
    calls: list[dict] = []
    _InlineThread.instances = []

    def fake_urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.get_full_url(),
                "timeout": timeout,
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        return _Response()

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:telegram-secret")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 4.0)
    monkeypatch.setattr(notify.threading, "Thread", _InlineThread)
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    notify.send_bg("quota_exhausted", "額度耗盡", account="prod")

    assert len(_InlineThread.instances) == 1
    assert _InlineThread.instances[0].daemon is True
    assert _InlineThread.instances[0].started is True
    assert calls == [
        {
            "url": "https://api.telegram.org/bot123:telegram-secret/sendMessage",
            "timeout": 4.0,
            "body": {
                "chat_id": "42",
                "text": "[ti] quota_exhausted：額度耗盡\naccount=prod",
                "disable_web_page_preview": True,
            },
        }
    ]
    assert "parse_mode" not in calls[0]["body"]
