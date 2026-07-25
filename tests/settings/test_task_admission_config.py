"""Task admission rollout mode has one validated, reloadable control point."""

from __future__ import annotations

import os

from studio import config, settings


def test_task_admission_defaults_to_shadow_and_rejects_unknown_mode(monkeypatch):
    monkeypatch.delenv("TI_TASK_ADMISSION", raising=False)
    config.reload()
    assert config.TASK_ADMISSION_MODE == "shadow"

    monkeypatch.setenv("TI_TASK_ADMISSION", "unexpected")
    config.reload()
    assert config.TASK_ADMISSION_MODE == "shadow"


def test_task_admission_mode_reload_and_settings_field(monkeypatch):
    original = os.environ.get("TI_TASK_ADMISSION")
    try:
        field = next(f for f in settings.FIELDS if f.env == "TI_TASK_ADMISSION")
        assert field.group == "Autopilot"
        assert field.kind == "select"
        assert field.options == ("off", "shadow", "enforce")

        for mode in field.options:
            monkeypatch.setenv("TI_TASK_ADMISSION", mode)
            config.reload()
            assert config.TASK_ADMISSION_MODE == mode
    finally:
        if original is None:
            monkeypatch.delenv("TI_TASK_ADMISSION", raising=False)
        else:
            monkeypatch.setenv("TI_TASK_ADMISSION", original)
        config.reload()
