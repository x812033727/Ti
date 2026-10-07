"""QA guard for notify timeout settings edge cases."""

from __future__ import annotations

import os

from studio import config, settings


def test_notify_timeout_blank_unsets_env_and_reloads_default(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TI_NOTIFY_TIMEOUT=2.5\n", encoding="utf-8")
    monkeypatch.setattr(settings, "env_path", lambda: str(env_file))
    monkeypatch.setenv("TI_NOTIFY_TIMEOUT", "2.5")
    config.reload()

    settings.update({"TI_NOTIFY_TIMEOUT": ""})

    assert "TI_NOTIFY_TIMEOUT" not in env_file.read_text(encoding="utf-8")
    assert "TI_NOTIFY_TIMEOUT" not in os.environ
    assert config.NOTIFY_TIMEOUT == 10.0


def test_notify_timeout_trims_ui_input_and_read_back_matches(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings, "env_path", lambda: str(env_file))
    monkeypatch.delenv("TI_NOTIFY_TIMEOUT", raising=False)
    config.reload()

    state = settings.update({"TI_NOTIFY_TIMEOUT": " 6.25 "})

    text = env_file.read_text(encoding="utf-8")
    fields = {field["env"]: field for field in state["fields"]}
    assert "TI_NOTIFY_TIMEOUT" in text
    assert "6.25" in text
    assert " 6.25 " not in text
    assert os.environ["TI_NOTIFY_TIMEOUT"] == "6.25"
    assert config.NOTIFY_TIMEOUT == 6.25
    assert fields["TI_NOTIFY_TIMEOUT"]["value"] == "6.25"
    assert fields["TI_NOTIFY_TIMEOUT"]["set"] is True


def test_notify_timeout_whitespace_only_unsets_existing_value(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TI_NOTIFY_TIMEOUT=4.0\n", encoding="utf-8")
    monkeypatch.setattr(settings, "env_path", lambda: str(env_file))
    monkeypatch.setenv("TI_NOTIFY_TIMEOUT", "4.0")
    config.reload()

    state = settings.update({"TI_NOTIFY_TIMEOUT": "   "})

    fields = {field["env"]: field for field in state["fields"]}
    assert "TI_NOTIFY_TIMEOUT" not in env_file.read_text(encoding="utf-8")
    assert "TI_NOTIFY_TIMEOUT" not in os.environ
    assert config.NOTIFY_TIMEOUT == 10.0
    assert fields["TI_NOTIFY_TIMEOUT"]["set"] is False


def test_bad_notify_timeout_does_not_block_valid_webhook_update(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TI_NOTIFY_TIMEOUT=3.0\nTI_NOTIFY_WEBHOOK=https://old.example/hook\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "env_path", lambda: str(env_file))
    monkeypatch.setenv("TI_NOTIFY_TIMEOUT", "3.0")
    monkeypatch.setenv("TI_NOTIFY_WEBHOOK", "https://old.example/hook")
    config.reload()

    settings.update(
        {
            "TI_NOTIFY_TIMEOUT": "not-a-number",
            "TI_NOTIFY_WEBHOOK": "https://new.example/hook",
        }
    )

    text = env_file.read_text(encoding="utf-8")
    assert "TI_NOTIFY_TIMEOUT=3.0" in text
    assert "https://new.example/hook" in text
    assert "https://old.example/hook" not in text
    assert os.environ["TI_NOTIFY_TIMEOUT"] == "3.0"
    assert os.environ["TI_NOTIFY_WEBHOOK"] == "https://new.example/hook"
    assert config.NOTIFY_TIMEOUT == 3.0
    assert config.NOTIFY_WEBHOOK == "https://new.example/hook"
