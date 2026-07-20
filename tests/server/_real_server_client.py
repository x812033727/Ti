"""Shared assertions for real-server smoke subprocess clients."""

from __future__ import annotations

import subprocess

import pytest

LOOPBACK_REFUSED_SKIP_REASON = "沙箱禁跨進程 loopback（client 連線被拒）——環境因素"
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "WS_PROXY",
    "WSS_PROXY",
    "SOCKS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ws_proxy",
    "wss_proxy",
    "socks_proxy",
    "no_proxy",
)


def scrub_proxy_env(env: dict[str, str]) -> None:
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)


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
