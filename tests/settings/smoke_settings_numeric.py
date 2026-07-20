"""Minimal smoke check for numeric settings update behavior.

Run from the repository root:
    env PYTHONDONTWRITEBYTECODE=1 python3 tests/settings/smoke_settings_numeric.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from studio import config, settings  # noqa: E402


def main() -> None:
    smoke_root = ROOT / ".settings_numeric_smoke"
    keys = [field.env for field in settings.FIELDS]
    saved_env = {key: os.environ.get(key) for key in keys}
    saved_project_root = config.PROJECT_ROOT

    shutil.rmtree(smoke_root, ignore_errors=True)
    smoke_root.mkdir()

    try:
        config.PROJECT_ROOT = smoke_root

        settings.update({"TI_CLARIFY_TIMEOUT": "0.5"})
        assert os.environ["TI_CLARIFY_TIMEOUT"] == "0.5"
        assert config.CLARIFY_TIMEOUT == 0.5

        key = "TI_AUTOPILOT_FOLLOWUP_MAX_PER_TASK"
        os.environ.pop(key, None)
        settings.update({key: "0.5"})
        assert key not in os.environ

        os.environ.pop("TI_CLARIFY_TIMEOUT", None)
        settings.update({"TI_CLARIFY_TIMEOUT": "nan"})
        assert "TI_CLARIFY_TIMEOUT" not in os.environ
        assert config.CLARIFY_TIMEOUT == 180.0
    finally:
        config.PROJECT_ROOT = saved_project_root
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.reload()
        shutil.rmtree(smoke_root, ignore_errors=True)

    print("settings numeric smoke ok")


if __name__ == "__main__":
    main()
