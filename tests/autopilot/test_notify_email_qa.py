from __future__ import annotations

import json

import pytest

from studio import config, notify


@pytest.fixture(autouse=True)
def _isolated_notify_state(tmp_path, monkeypatch):
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


def _enable_email(monkeypatch, *, password: str = "smtp-secret-value"):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "alerts@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", password)
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Alerts <alerts@example.test>")


def test_email_blank_recipients_do_not_start_background_thread(monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", " , \t, ")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "ALERT_FROM", "alerts@example.test")
    monkeypatch.setattr(
        notify.threading.Thread,
        "start",
        lambda *_args, **_kwargs: pytest.fail("email sink should not be configured"),
    )

    notify.send_bg("task_failed", "blank recipients")

    deliveries = notify.read_deliveries(1)
    assert [(row["sink"], row["error"]) for row in deliveries] == [("none", "not_configured")]


def test_email_failure_does_not_mask_webhook_success(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example.test/ti")
    _enable_email(monkeypatch)
    webhook_bodies: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout=None):
        webhook_bodies.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    def smtp_down(*_args, **_kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp_down)

    assert notify.send("loop_stall", "main loop stalled") is True

    assert webhook_bodies and webhook_bodies[0]["kind"] == "loop_stall"
    deliveries = notify.read_deliveries(1)
    assert [(row["sink"], row["ok"], row["error"]) for row in deliveries] == [
        ("webhook", True, ""),
        ("email", False, "delivery_failed"),
    ]


def test_smtp_password_never_appears_in_debug_logs(monkeypatch, caplog):
    secret = "smtp-secret-value"
    _enable_email(monkeypatch, password=secret)

    class _LeakySMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            pass

        def login(self, _user, password):
            raise RuntimeError(f"auth rejected password={password}")

    monkeypatch.setattr(notify.smtplib, "SMTP", _LeakySMTP)
    caplog.set_level("DEBUG", logger="ti.notify")

    assert notify.send("task_failed", "smtp auth rejected") is False

    assert secret not in caplog.text
