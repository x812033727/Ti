"""Email 通知 sink：SMTP 設定齊全才啟用，與其他 sink 獨立成敗。"""

from __future__ import annotations

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
    monkeypatch.setattr(config, "ALERT_FROM", "")
    return tmp_path


class _SMTPFake:
    calls: list[tuple] = []

    def __init__(self, host, port, timeout=None):
        self.calls.append(("connect", self.__class__.__name__, host, port, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, sender, recipients, body):
        self.calls.append(("sendmail", sender, recipients, body))


def _configure_email(monkeypatch, *, port=587):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test, dev@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", port)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "alerts@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "secret")
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Alerts <alerts@example.test>")


def _decoded_body(raw: str) -> str:
    msg = message_from_string(raw)
    payload = msg.get_payload(decode=True)
    return (
        payload.decode(msg.get_content_charset() or "utf-8")
        if isinstance(payload, bytes)
        else str(payload)
    )


def test_email_requires_host_from_and_recipient(monkeypatch):
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_FROM", "alerts@example.test")
    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *_args, **_kwargs: pytest.fail("no smtp"))

    assert notify.send("task_failed", "x") is False


def test_email_port_465_uses_smtp_ssl(monkeypatch):
    _SMTPFake.calls = []
    _configure_email(monkeypatch, port=465)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _SMTPFake)
    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *_args, **_kwargs: pytest.fail("wrong smtp"))

    assert notify.send("quota_exhausted", "額度耗盡", account="B") is True

    assert ("connect", "_SMTPFake", "smtp.example.test", 465, 10.0) in _SMTPFake.calls
    assert ("login", "alerts@example.test", "secret") in _SMTPFake.calls
    sent = [call for call in _SMTPFake.calls if call[0] == "sendmail"][0]
    assert sent[1] == "Ti Alerts <alerts@example.test>"
    assert sent[2] == ["ops@example.test", "dev@example.test"]
    body = _decoded_body(sent[3])
    assert "quota_exhausted" in body
    assert "account=B" in body
    assert not any(call[0] == "starttls" for call in _SMTPFake.calls)


def test_email_non_465_uses_starttls(monkeypatch):
    _SMTPFake.calls = []
    _configure_email(monkeypatch, port=587)
    monkeypatch.setattr(notify.smtplib, "SMTP", _SMTPFake)
    monkeypatch.setattr(
        notify.smtplib, "SMTP_SSL", lambda *_args, **_kwargs: pytest.fail("wrong smtp ssl")
    )

    assert notify.send("loop_stall", "停滯") is True

    assert ("connect", "_SMTPFake", "smtp.example.test", 587, 10.0) in _SMTPFake.calls
    assert ("starttls",) in _SMTPFake.calls
    assert any(call[0] == "sendmail" for call in _SMTPFake.calls)


def test_email_swallows_smtp_errors(monkeypatch):
    _configure_email(monkeypatch, port=465)

    def boom(*_args, **_kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", boom)

    assert notify.send("task_failed", "x") is False
