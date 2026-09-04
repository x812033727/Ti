"""Email 推播 sink：SMTP_SSL/STARTTLS、零設定零網路、例外不外拋。"""

from __future__ import annotations

import json
import logging

import pytest

from studio import config, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
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
    return tmp_path


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_call = None
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_call = (user, password)

    def send_message(self, message, to_addrs=None):
        self.sent.append({"message": message, "to_addrs": list(to_addrs or [])})


def _set_email(monkeypatch, *, port=587, user="", password=""):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.com, qa@example.com")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", port)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", user)
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", password)
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Studio <ti@example.com>")
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 4.5)


def test_email_disabled_without_host_or_recipient(monkeypatch):
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP",
        lambda *_args, **_kwargs: pytest.fail("未設定 email sink 不得碰 SMTP"),
    )
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("未設定 email sink 不得碰 SMTP_SSL"),
    )

    assert notify.send("task_failed", "任務失敗") is False
    assert notify.send_test() == {"ok": False, "sinks": {}}


def test_email_465_uses_smtp_ssl_and_payload(monkeypatch):
    _set_email(monkeypatch, port=465, user="bot@example.com", password="app-pass")
    instances = []

    def fake_ssl(host, port, timeout=None):
        smtp = FakeSMTP(host, port, timeout)
        instances.append(smtp)
        return smtp

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", fake_ssl)
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP",
        lambda *_args, **_kwargs: pytest.fail("465 應走 SMTP_SSL"),
    )

    assert notify.send("quota_exhausted", "額度耗盡", account="B") is True

    smtp = instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.test", 465, 4.5)
    assert smtp.started_tls is False
    assert smtp.login_call == ("bot@example.com", "app-pass")
    sent = smtp.sent[0]
    assert sent["to_addrs"] == ["ops@example.com", "qa@example.com"]
    message = sent["message"]
    assert message["From"] == "Ti Studio <ti@example.com>"
    assert message["To"] == "ops@example.com, qa@example.com"
    assert message["Subject"] == "[ti] quota_exhausted: 額度耗盡"
    body = json.loads(message.get_content())
    assert body == {
        "source": "ti",
        "kind": "quota_exhausted",
        "title": "額度耗盡",
        "account": "B",
    }


def test_email_587_uses_starttls_without_requiring_login(monkeypatch):
    _set_email(monkeypatch, port=587)
    instances = []

    def fake_smtp(host, port, timeout=None):
        smtp = FakeSMTP(host, port, timeout)
        instances.append(smtp)
        return smtp

    monkeypatch.setattr(notify.smtplib, "SMTP", fake_smtp)
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("587 不應走 SMTP_SSL"),
    )

    assert notify.send("loop_stall", "停滯") is True

    smtp = instances[0]
    assert smtp.started_tls is True
    assert smtp.login_call is None
    assert smtp.sent[0]["to_addrs"] == ["ops@example.com", "qa@example.com"]


def test_email_failure_is_swallowed_and_secret_not_logged(monkeypatch, caplog):
    _set_email(monkeypatch, port=465, user="bot@example.com", password="super-secret")

    def boom(*_args, **_kwargs):
        raise OSError("smtp unavailable")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", boom)
    caplog.set_level(logging.DEBUG, logger="ti.notify")

    assert notify.send("task_failed", "任務失敗") is False
    assert "super-secret" not in caplog.text
    assert "smtp.example.test" not in caplog.text


def test_send_bg_starts_thread_when_only_email_is_configured(monkeypatch):
    _set_email(monkeypatch)
    threads = []
    deliveries = []

    class FakeThread:
        def __init__(self, target, args=(), kwargs=None, daemon=False, **_ignored):
            self.target = target
            self.args = args
            self.kwargs = {} if kwargs is None else kwargs
            self.daemon = daemon
            threads.append(self)

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(notify.threading, "Thread", FakeThread)
    monkeypatch.setattr(notify, "_post_email", lambda *args: deliveries.append(args) or True)

    notify.send_bg("task_failed", "任務失敗", task_id=7)

    assert len(threads) == 1
    assert threads[0].daemon is True
    assert deliveries and deliveries[0][-3:] == ("task_failed", "任務失敗", {"task_id": 7})
