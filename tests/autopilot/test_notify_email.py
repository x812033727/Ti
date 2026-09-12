"""Email 通知 sink：SMTP_SSL/STARTTLS、no-op 與失敗吞掉。"""

from __future__ import annotations

import pytest

from studio import config, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "")
    monkeypatch.setattr(config, "ALERT_EMAIL_FROM", "")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    return tmp_path


class _FakeSMTP:
    calls: list[tuple] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls.append(("connect", host, port, timeout))

    def __enter__(self):
        self.calls.append(("enter", self.host, self.port))
        return self

    def __exit__(self, *args):
        self.calls.append(("exit", self.host, self.port))
        return False

    def starttls(self):
        self.calls.append(("starttls", self.host, self.port))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(
            ("send_message", msg["From"], msg["To"], msg["Subject"], msg.get_content())
        )


def _enable_email(monkeypatch, *, port: int = 587):
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", port)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "bot@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "smtp-secret")
    monkeypatch.setattr(config, "ALERT_EMAIL_FROM", "ti@example.test")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")


def test_email_requires_host_and_recipient(monkeypatch):
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP",
        lambda *_args, **_kwargs: pytest.fail("未完整設定 email 時不得連 SMTP"),
    )
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("未完整設定 email 時不得連 SMTP_SSL"),
    )

    assert notify.send("task_failed", "x") is False
    assert notify.send_test() == {"ok": False, "sinks": {}}


def test_email_port_465_uses_smtp_ssl(monkeypatch):
    _enable_email(monkeypatch, port=465)
    _FakeSMTP.calls = []
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP",
        lambda *_args, **_kwargs: pytest.fail("465 必須走 SMTP_SSL"),
    )

    assert notify.send("quota_exhausted", "額度耗盡", account="B") is True

    assert ("connect", "smtp.example.test", 465, 10.0) in _FakeSMTP.calls
    assert ("login", "bot@example.test", "smtp-secret") in _FakeSMTP.calls
    send = next(call for call in _FakeSMTP.calls if call[0] == "send_message")
    assert send[1] == "ti@example.test" and send[2] == "ops@example.test"
    assert "quota_exhausted" in send[3] and "額度耗盡" in send[4]
    assert "account=B" in send[4]


def test_email_port_587_uses_starttls(monkeypatch):
    _enable_email(monkeypatch, port=587)
    _FakeSMTP.calls = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(
        notify.smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("587 不得走 SMTP_SSL"),
    )

    assert notify.send("loop_stall", "停滯") is True

    assert ("connect", "smtp.example.test", 587, 10.0) in _FakeSMTP.calls
    assert ("starttls", "smtp.example.test", 587) in _FakeSMTP.calls


def test_email_invalid_port_falls_back_to_starttls(monkeypatch):
    _enable_email(monkeypatch)
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", "bad-port")
    _FakeSMTP.calls = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)

    assert notify.send("loop_stall", "停滯") is True

    assert ("connect", "smtp.example.test", 587, 10.0) in _FakeSMTP.calls
    assert ("starttls", "smtp.example.test", 587) in _FakeSMTP.calls


def test_email_failure_is_swallowed_and_recorded(monkeypatch):
    _enable_email(monkeypatch)

    class BoomSMTP(_FakeSMTP):
        def send_message(self, msg):
            raise OSError("smtp boom")

    monkeypatch.setattr(notify.smtplib, "SMTP", BoomSMTP)

    assert notify.send("task_failed", "x") is False
    row = notify.read_deliveries(1)[0]
    assert row["sink"] == "email"
    assert row["ok"] is False
    assert row["error"] == "delivery_failed"


def test_send_bg_uses_thread_when_only_email_configured(monkeypatch):
    _enable_email(monkeypatch)
    threads = []

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

    monkeypatch.setattr(notify.threading, "Thread", FakeThread)
    notify.send_bg("provider_unavailable", "quota")

    assert len(threads) == 1
    assert threads[0].daemon is True
    assert threads[0].started is True
