"""Notify 設定鍵：config.reload() 讀 env。"""

from __future__ import annotations

import os

from studio import config, settings


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


def test_notify_timeout_is_ui_allowed_float(monkeypatch, tmp_path):
    keys = ("TI_NOTIFY_TIMEOUT",)
    saved = {key: os.environ.get(key) for key in keys}
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings, "env_path", lambda: str(env_file))
    try:
        os.environ.pop("TI_NOTIFY_TIMEOUT", None)
        config.reload()

        fields = {field["env"]: field for field in settings.read()["fields"]}
        assert fields["TI_NOTIFY_TIMEOUT"]["group"] == "通知"

        settings.update({"TI_NOTIFY_TIMEOUT": "2.5"})
        assert os.environ["TI_NOTIFY_TIMEOUT"] == "2.5"
        assert config.NOTIFY_TIMEOUT == 2.5
        assert "TI_NOTIFY_TIMEOUT" in env_file.read_text(encoding="utf-8")

        settings.update({"TI_NOTIFY_TIMEOUT": "abc"})
        assert os.environ["TI_NOTIFY_TIMEOUT"] == "2.5"
        assert config.NOTIFY_TIMEOUT == 2.5

        for bad in ("nan", "inf", "0", "-1"):
            settings.update({"TI_NOTIFY_TIMEOUT": bad})
            assert os.environ["TI_NOTIFY_TIMEOUT"] == "2.5"
            assert config.NOTIFY_TIMEOUT == 2.5
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.reload()
