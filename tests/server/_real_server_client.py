"""Shared assertions for real-server smoke subprocess clients."""

from __future__ import annotations

import subprocess

import pytest

LOOPBACK_REFUSED_SKIP_REASON = "沙箱禁跨進程 loopback（client 連線被拒）——環境因素"


def _looks_like_loopback_refused_traceback(output: str) -> bool:
    if "SMOKE FAIL" in output or "Traceback" not in output or "127.0.0.1" not in output:
        return False
    return "ConnectionRefusedError" in output or (
        "OSError" in output and "Connect call failed" in output
    )


def assert_smoke_client_ok(
    client: subprocess.CompletedProcess[str],
    label: str,
    server_log_tail: str,
) -> None:
    if client.returncode == 0:
        return

    output = f"{client.stdout}\n{client.stderr}"
    if _looks_like_loopback_refused_traceback(output):
        pytest.skip(LOOPBACK_REFUSED_SKIP_REASON)

    pytest.fail(
        f"{label} FAIL（rc={client.returncode}）：\n"
        f"{client.stdout}\n{client.stderr}\n--- server log ---\n{server_log_tail}",
        pytrace=False,
    )
