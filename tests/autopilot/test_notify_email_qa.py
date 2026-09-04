"""QA：Email sink 的交互失敗、收件人邊界與敏感資料遮蔽。"""

from __future__ import annotations

import json

import pytest

from studio import config, notify


@pytest.fixture(autouse=True)
def _clean_notify_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "")
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Studio <noreply>")
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 2.0)
    return tmp_path


class _FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        self.started_tls = True

    def send_message(self, message, to_addrs=None):
        self.sent.append((message, list(to_addrs or [])))


def _enable_email(monkeypatch, *, recipients="ops@example.com", password=""):
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", recipients)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", password)
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Ops <ti@example.com>")


def test_email_recipient_parser_accepts_semicolon_and_drops_empty_slots(monkeypatch):
    _enable_email(
        monkeypatch,
        recipients=" ops@example.com ; ; qa@example.com,release@example.com ",
    )
    instances = []

    def fake_smtp(host, port, timeout=None):
        smtp = _FakeSMTP(host, port, timeout)
        instances.append(smtp)
        return smtp

    monkeypatch.setattr(notify.smtplib, "SMTP", fake_smtp)
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("587 不得走 SMTP_SSL"),
    )

    assert notify.send("task_failed", "收件人解析") is True

    smtp = instances[0]
    assert smtp.started_tls is True
    message, to_addrs = smtp.sent[0]
    assert to_addrs == ["ops@example.com", "qa@example.com", "release@example.com"]
    assert message["To"] == "ops@example.com, qa@example.com, release@example.com"


def test_email_payload_redacts_secret_marked_fields(monkeypatch):
    _enable_email(monkeypatch, password="smtp-secret")
    instances = []

    def fake_smtp(host, port, timeout=None):
        smtp = _FakeSMTP(host, port, timeout)
        instances.append(smtp)
        return smtp

    monkeypatch.setattr(notify.smtplib, "SMTP", fake_smtp)

    assert notify.send(
        "task_failed",
        "秘密不該外洩 smtp-secret",
        smtp_pass="smtp-secret",
        nested={"authorization": "bearer-token"},
    )

    message, _to_addrs = instances[0].sent[0]
    payload = json.loads(message.get_content())
    assert payload["title"] == "秘密不該外洩 ***"
    assert payload["smtp_pass"] == {"configured": True}
    assert payload["nested"]["authorization"] == {"configured": True}
    assert "smtp-secret" not in message.get_content()


def test_webhook_success_with_email_failure_still_returns_success_and_records_both(
    monkeypatch,
):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hooks.example.test/notify")
    _enable_email(monkeypatch)
    webhook_calls = []

    def fake_urlopen(request, timeout):
        webhook_calls.append((request.get_full_url(), timeout, json.loads(request.data)))

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return _Response()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify, "_post_email", lambda *_args: False)

    assert notify.send("quota_exhausted", "額度耗盡", account="B") is True

    assert webhook_calls == [
        (
            "https://hooks.example.test/notify",
            2.0,
            {
                "source": "ti",
                "kind": "quota_exhausted",
                "title": "額度耗盡",
                "account": "B",
            },
        )
    ]
    deliveries = sorted(notify.read_deliveries(1), key=lambda item: item["sink"])
    assert [(item["sink"], item["ok"], item["error"]) for item in deliveries] == [
        ("email", False, "delivery_failed"),
        ("webhook", True, ""),
    ]
