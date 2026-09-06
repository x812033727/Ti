"""Email 通知 sink：SMTP_SSL/STARTTLS 分流、失敗吞掉、背景送出。"""

from __future__ import annotations

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


def _enable_email(monkeypatch, *, port=587):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", port)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "bot@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "app-secret")
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Alerts <bot@example.test>")


def _capture_smtp(monkeypatch, *, fail_message: str | None = None):
    calls: list[object] = []

    class BaseSMTP:
        mode = "plain"

        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.starttls_called = False
            self.login_calls: list[tuple[str, str]] = []
            self.messages = []
            calls.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self):
            self.starttls_called = True

        def login(self, user, password):
            self.login_calls.append((user, password))

        def send_message(self, message):
            self.messages.append(message)
            if fail_message:
                raise OSError(fail_message)

    class PlainSMTP(BaseSMTP):
        mode = "plain"

    class SslSMTP(BaseSMTP):
        mode = "ssl"

    monkeypatch.setattr(notify.smtplib, "SMTP", PlainSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", SslSMTP)
    return calls


def test_email_disabled_without_host_or_recipient_does_not_touch_smtp(monkeypatch):
    calls = _capture_smtp(monkeypatch)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")

    assert notify.send("task_failed", "x") is False
    assert calls == []


def test_email_port_465_uses_smtp_ssl(monkeypatch):
    _enable_email(monkeypatch, port=465)
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 2.5)
    calls = _capture_smtp(monkeypatch)

    assert notify.send("quota_exhausted", "額度耗盡", account="B") is True

    smtp = calls[0]
    assert smtp.mode == "ssl"
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.test", 465, 2.5)
    assert smtp.starttls_called is False
    assert smtp.login_calls == [("bot@example.test", "app-secret")]
    message = smtp.messages[0]
    assert message["From"] == "Ti Alerts <bot@example.test>"
    assert message["To"] == "ops@example.test"
    assert message["Subject"] == "[ti] quota_exhausted: 額度耗盡"
    assert "account=B" in message.get_content()


def test_email_port_587_uses_starttls(monkeypatch):
    _enable_email(monkeypatch, port=587)
    calls = _capture_smtp(monkeypatch)

    assert notify.send("loop_stall", "停滯") is True

    smtp = calls[0]
    assert smtp.mode == "plain"
    assert smtp.starttls_called is True
    assert smtp.login_calls == [("bot@example.test", "app-secret")]


def test_email_swallows_smtp_errors_without_logging_secret(monkeypatch, caplog):
    _enable_email(monkeypatch, port=587)
    calls = _capture_smtp(monkeypatch, fail_message="boom app-secret")
    caplog.set_level(logging.DEBUG, logger="ti.notify")

    assert notify.send("task_failed", "失敗") is False

    assert len(calls) == 1
    assert "app-secret" not in caplog.text
    assert "smtp.example.test" not in caplog.text


def test_send_test_reports_email_sink(monkeypatch):
    _enable_email(monkeypatch, port=587)
    _capture_smtp(monkeypatch)

    assert notify.send_test() == {"ok": True, "sinks": {"email": True}}


def test_send_bg_email_only_starts_daemon_thread(monkeypatch):
    _enable_email(monkeypatch, port=587)
    calls = _capture_smtp(monkeypatch)
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

    notify.send_bg("task_failed", "失敗", task_id=1)

    assert len(threads) == 1
    assert threads[0].daemon is True
    assert threads[0].started is True
    assert len(calls) == 1


def test_send_bg_without_any_sink_does_not_start_thread(monkeypatch):
    threads: list[object] = []
    monkeypatch.setattr(notify.threading, "Thread", lambda *args, **kwargs: threads.append(args))
    notify.send_bg("task_failed", "失敗")
    assert threads == []
