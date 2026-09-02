"""Email notify sink：SMTP_SSL/STARTTLS 雙路、非阻塞、失敗不影響主流程。"""

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
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 10.0)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "")
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Studio <noreply@localhost>")
    return tmp_path


def _smtp_fakes(monkeypatch, calls: list[dict]):
    def _make(name):
        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.call = {
                    "client": name,
                    "host": host,
                    "port": port,
                    "timeout": timeout,
                    "starttls": False,
                    "login": None,
                    "sendmail": None,
                }
                calls.append(self.call)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def starttls(self):
                self.call["starttls"] = True

            def login(self, user, password):
                self.call["login"] = (user, password)

            def sendmail(self, sender, recipients, message):
                self.call["sendmail"] = (sender, recipients, message)

        return FakeSMTP

    monkeypatch.setattr(notify.smtplib, "SMTP", _make("SMTP"))
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _make("SMTP_SSL"))


def _message_body(raw: str) -> str:
    msg = message_from_string(raw)
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8")
    return str(payload)


def test_email_not_configured_does_not_touch_smtp(monkeypatch):
    def fail_smtp(*_args, **_kwargs):
        pytest.fail("SMTP must not be opened when email sink is not configured")

    monkeypatch.setattr(notify.smtplib, "SMTP", fail_smtp)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", fail_smtp)

    assert notify.send("quota_exhausted", "額度耗盡") is False


def test_sinks_configured_includes_email_only(monkeypatch):
    assert notify.sinks_configured() is False
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    assert notify.sinks_configured() is False, "缺收件人不算啟用"
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", " , ")
    assert notify.email_configured() is False
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    assert notify.email_configured() is True
    assert notify.sinks_configured() is True


def test_email_port_465_uses_smtp_ssl(monkeypatch):
    calls: list[dict] = []
    _smtp_fakes(monkeypatch, calls)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test, dev@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 465)
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Bot <bot@example.test>")

    assert notify.send("quota_exhausted", "額度耗盡", account="B") is True

    assert len(calls) == 1
    call = calls[0]
    assert call["client"] == "SMTP_SSL"
    assert call["host"] == "smtp.example.test"
    assert call["port"] == 465
    assert call["timeout"] == 10.0
    assert call["starttls"] is False
    sender, recipients, message = call["sendmail"]
    assert sender == "Ti Bot <bot@example.test>"
    assert recipients == ["ops@example.test", "dev@example.test"]
    body = _message_body(message)
    assert "kind: quota_exhausted" in body
    assert "account: B" in body


def test_email_port_587_uses_starttls_login_and_timeout(monkeypatch):
    calls: list[dict] = []
    _smtp_fakes(monkeypatch, calls)
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 4.25)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "apikey")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "smtp-secret")

    assert notify.send("loop_stall", "停滯") is True

    call = calls[0]
    assert call["client"] == "SMTP"
    assert call["timeout"] == 4.25
    assert call["starttls"] is True
    assert call["login"] == ("apikey", "smtp-secret")


def test_email_errors_are_swallowed_and_secret_not_logged(monkeypatch, caplog):
    class FailingSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            raise OSError("smtp down")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "smtp-secret")
    monkeypatch.setattr(notify.smtplib, "SMTP", FailingSMTP)
    caplog.set_level(logging.DEBUG, logger="ti.notify")

    assert notify.send("loop_stall", "停滯") is False
    assert "smtp-secret" not in caplog.text
    assert "smtp.example.test" not in caplog.text


def test_send_bg_starts_thread_when_only_email_configured(monkeypatch):
    calls: list[dict] = []
    threads: list[object] = []
    _smtp_fakes(monkeypatch, calls)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")

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

    notify.send_bg("loop_stall", "停滯")

    assert len(threads) == 1
    assert threads[0].daemon is True
    assert threads[0].started is True
    assert calls and calls[0]["client"] == "SMTP"


def test_send_test_reports_email_sink(monkeypatch):
    calls: list[dict] = []
    _smtp_fakes(monkeypatch, calls)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")

    assert notify.send_test() == {"ok": True, "sinks": {"email": True}}
    assert calls and calls[0]["sendmail"] is not None


def test_send_bg_all_sinks_empty_stays_noop(monkeypatch):
    class NoThread:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("send_bg must not start a thread when all sinks are empty")

    monkeypatch.setattr(notify.threading, "Thread", NoThread)

    notify.send_bg("loop_stall", "停滯")
