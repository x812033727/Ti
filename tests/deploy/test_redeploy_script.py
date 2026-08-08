"""後備 redeploy shell 腳本的靜態守護。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_redeploy_script_uses_portable_serve_launcher():
    text = (ROOT / "scripts" / "redeploy.sh").read_text(encoding="utf-8")

    assert "exec bash scripts/serve.sh" in text
    assert "exec python -m studio.server" not in text
