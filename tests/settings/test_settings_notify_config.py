"""Notify 設定鍵：config.reload() 讀 env。"""

from __future__ import annotations

import os

from studio import config


def test_notify_webhook_and_timeout_reload_from_env():
    keys = ("TI_NOTIFY_WEBHOOK", "TI_NOTIFY_TIMEOUT")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["TI_NOTIFY_WEBHOOK"] = "https://hooks.example.test/notify"
        os.environ["TI_NOTIFY_TIMEOUT"] = "7.5"
        config.reload()

        assert config.NOTIFY_WEBHOOK == "https://hooks.example.test/notify"
        assert config.NOTIFY_TIMEOUT == 7.5
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.reload()
