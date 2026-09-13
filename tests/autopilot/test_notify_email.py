"""Email notification sink: SMTP_SSL/STARTTLS, no network in tests."""

from __future__ import annotations

import logging
from email import message_from_string

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
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Studio <noreply@localhost>")
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 4.0)
    return tmp_path


def _smtp_spy(monkeypatch):
    calls: list[dict] = []

    class FakeSMTP:
        scheme = "smtp"

        def __init__(self, host, port, timeout=None):
            self.record = {
                "scheme": self.scheme,
                "host": host,
                "port": port,
                "timeout": timeout,
                "events": [],
            }
            calls.append(self.record)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            self.record["events"].append(("starttls",))

        def login(self, user, password):
            self.record["events"].append(("login", user, password))

        def sendmail(self, sender, recipients, message):
            self.record["events"].append(("sendmail", sender, recipients, message))

    class FakeSMTPSSL(FakeSMTP):
        scheme = "ssl"

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return calls


def test_email_not_configured_is_noop(monkeypatch):
    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *_a, **_kw: pytest.fail("no SMTP"))
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", lambda *_a, **_kw: pytest.fail("no SMTP_SSL"))

    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    assert notify.send("task_failed", "failed") is False

    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    assert notify.send("task_failed", "failed") is False


def test_email_port_465_uses_smtp_ssl(monkeypatch):
    calls = _smtp_spy(monkeypatch)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test, dev@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 465)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "smtp-user")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "smtp-pass")
    monkeypatch.setattr(config, "ALERT_FROM", "Ti <ti@example.test>")

    assert notify.send("quota_exhausted", "budget reached", task_id=7) is True

    assert calls[0]["scheme"] == "ssl"
    assert calls[0]["host"] == "smtp.example.test"
    assert calls[0]["port"] == 465
    assert calls[0]["timeout"] == 4.0
    assert ("starttls",) not in calls[0]["events"]
    assert ("login", "smtp-user", "smtp-pass") in calls[0]["events"]
    sendmail = calls[0]["events"][-1]
    assert sendmail[:3] == (
        "sendmail",
        "Ti <ti@example.test>",
        ["ops@example.test", "dev@example.test"],
    )
    assert "Subject: [ti] quota_exhausted: budget reached" in sendmail[3]
    parsed = message_from_string(sendmail[3])
    body = parsed.get_payload(decode=True).decode(parsed.get_content_charset() or "utf-8")
    assert "task_id=7" in body


def test_email_port_587_uses_starttls(monkeypatch):
    calls = _smtp_spy(monkeypatch)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "smtp-user")

    assert notify.send("loop_stall", "stalled") is True

    assert calls[0]["scheme"] == "smtp"
    assert calls[0]["events"][0] == ("starttls",)
    assert calls[0]["events"][1] == ("login", "smtp-user", "")


def test_email_errors_are_swallowed_without_logging_password(monkeypatch, caplog):
    class BoomSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            pass

        def sendmail(self, *_args):
            raise OSError("smtp rejected secret-pass")

    monkeypatch.setattr(notify.smtplib, "SMTP", BoomSMTP)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "secret-pass")
    caplog.set_level(logging.DEBUG, logger="ti.notify")

    assert notify.send("task_failed", "failed") is False
    assert "secret-pass" not in caplog.text


def test_send_bg_email_only_starts_daemon_thread(monkeypatch):
    calls = _smtp_spy(monkeypatch)
    threads: list[object] = []

    class FakeThread:
        def __init__(self, target, args=(), kwargs=None, daemon=False, **_ignored):
            self.target = target
            self.args = args
            self.kwargs = {} if kwargs is None else kwargs
            self.daemon = daemon
            self.started = False
            threads.append(self)

        def start(self):
            self.started = True
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(notify.threading, "Thread", FakeThread)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")

    notify.send_bg("task_failed", "failed", task_id=1)

    assert len(threads) == 1
    assert threads[0].daemon is True
    assert threads[0].started is True
    assert calls and calls[0]["events"][-1][0] == "sendmail"
