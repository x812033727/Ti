"""QA evidence test for ws-attach real-server client loopback refusal."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("websockets")
pytest.importorskip("httpx")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tests" / "server" / "smoke_ws_attach_real_server.py"
EVIDENCE_PATH = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "ws_attach_evidence.txt"


def _run_client_against_refused_loopback() -> tuple[int, subprocess.CompletedProcess[str]]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as guard:
        guard.bind(("127.0.0.1", 0))
        port = guard.getsockname()[1]
        client = subprocess.run(
            [sys.executable, str(SCRIPT), str(port)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return port, client


def _environment_failure_features(port: int) -> tuple[str, ...]:
    return (
        "Traceback (most recent call last):",
        "raise OSError(err, f'Connect call failed {address}')",
        f"ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', {port})",
    )


def _write_evidence(port: int, client: subprocess.CompletedProcess[str]) -> None:
    features = _environment_failure_features(port)
    EVIDENCE_PATH.write_text(
        "\n".join(
            [
                "## ENV",
                f"TMPDIR={EVIDENCE_PATH.parent}",
                f"PWD={ROOT}",
                f"PYTHON={sys.version.split()[0]}",
                "",
                "## RUN smoke client against closed loopback port",
                f"CLOSED_PORT={port}",
                f"COMMAND: timeout 60 {sys.executable} {SCRIPT.relative_to(ROOT)} {port}",
                "",
                "## STDOUT",
                client.stdout,
                "## STDERR/TRACEBACK",
                client.stderr,
                f"CLIENT_RC={client.returncode}",
                "",
                "## environment failure feature strings",
                *[f"- {feature}" for feature in features],
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_task1_ws_attach_client_refused_traceback_is_persisted_to_tmpdir() -> None:
    port, client = _run_client_against_refused_loopback()
    features = _environment_failure_features(port)
    combined_output = f"{client.stdout}\n{client.stderr}"

    assert client.returncode != 0
    assert "SMOKE FAIL" not in combined_output
    for feature in features:
        assert feature in combined_output
    assert "websockets/asyncio/client.py" in client.stderr
    assert "asyncio/selector_events.py" in client.stderr

    _write_evidence(port, client)
    evidence = EVIDENCE_PATH.read_text(encoding="utf-8")

    assert f"CLIENT_RC={client.returncode}" in evidence
    assert client.stderr in evidence
    for feature in features:
        assert f"- {feature}" in evidence
