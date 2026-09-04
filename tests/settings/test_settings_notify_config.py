"""Notify 設定鍵：config.reload() 讀 env。"""

from __future__ import annotations

import os

from studio import config


def test_notify_webhook_and_timeout_reload_from_env():
    keys = (
        "TI_NOTIFY_WEBHOOK",
        "TI_NOTIFY_TIMEOUT",
        "TI_ALERT_EMAIL_TO",
        "TI_ALERT_SMTP_HOST",
        "TI_ALERT_SMTP_PORT",
        "TI_ALERT_SMTP_USER",
        "TI_ALERT_SMTP_PASS",
        "TI_ALERT_FROM",
    )
    saved = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["TI_NOTIFY_WEBHOOK"] = "https://hooks.example.test/notify"
        os.environ["TI_NOTIFY_TIMEOUT"] = "7.5"
        os.environ["TI_ALERT_EMAIL_TO"] = "ops@example.com"
        os.environ["TI_ALERT_SMTP_HOST"] = "smtp.example.test"
        os.environ["TI_ALERT_SMTP_PORT"] = "2525"
        os.environ["TI_ALERT_SMTP_USER"] = "bot@example.com"
        os.environ["TI_ALERT_SMTP_PASS"] = "app-pass"
        os.environ["TI_ALERT_FROM"] = "Ti Ops <ti@example.com>"
        config.reload()

        assert config.NOTIFY_WEBHOOK == "https://hooks.example.test/notify"
        assert config.NOTIFY_TIMEOUT == 7.5
        assert config.ALERT_EMAIL_TO == "ops@example.com"
        assert config.ALERT_SMTP_HOST == "smtp.example.test"
        assert config.ALERT_SMTP_PORT == 2525
        assert config.ALERT_SMTP_USER == "bot@example.com"
        assert config.ALERT_SMTP_PASS == "app-pass"
        assert config.ALERT_FROM == "Ti Ops <ti@example.com>"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.reload()
