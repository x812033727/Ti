"""Guard tests for real-server smoke subprocess result classification."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _pytest.outcomes import Failed
from _real_server_client import LOOPBACK_REFUSED_SKIP_REASON, assert_smoke_client_ok

HERE = Path(__file__).resolve().parent


def _client(
    stdout: str = "", stderr: str = "", returncode: int = 1
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "tests/server/smoke_agenda_real_server.py", "9"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode"),
    [
        (
            "PASS - 收到 attach_ok\nSMOKE FAIL: ['事件總數對帳（拼接 7 vs JSONL 8）']\n",
            "",
            1,
        ),
        (
            "PASS - 恰一筆 agenda_plan 事件\nFAIL - history 重播事件數一致: 0 vs 12\n",
            "",
            2,
        ),
        (
            "",
            "\n".join(
                [
                    "Traceback (most recent call last):",
                    '  File "tests/server/smoke_agenda_real_server.py", line 80, in main',
                    '    assert "SMOKE PASS" in client.stdout',
                    "AssertionError: SMOKE PASS missing for http://127.0.0.1:8021",
                ]
            ),
            1,
        ),
        (
            "",
            "\n".join(
                [
                    "Traceback (most recent call last):",
                    '  File "tests/server/smoke_ws_attach_real_server.py", line 95, in main',
                    '    sid = done["session_id"]',
                    "KeyError: 'session_id'",
                ]
            ),
            1,
        ),
    ],
)
def test_non_loopback_refused_smoke_errors_are_hard_failures(
    stdout: str,
    stderr: str,
    returncode: int,
) -> None:
    with pytest.raises(Failed):
        assert_smoke_client_ok(
            _client(stdout=stdout, stderr=stderr, returncode=returncode),
            "QA guard",
            "Uvicorn running on http://127.0.0.1:8021",
        )


@pytest.mark.parametrize(
    "stderr",
    [
        "\n".join(
            [
                "Traceback (most recent call last):",
                '  File "<stdin>", line 6, in main',
                '  File "/usr/local/lib/python3.12/dist-packages/websockets/asyncio/client.py", line 590, in __aenter__',
                "    return await self",
                "           ^^^^^^^^^^",
                '  File "/usr/lib/python3.12/asyncio/selector_events.py", line 691, in _sock_connect_cb',
                "    raise OSError(err, f'Connect call failed {address}')",
                "ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 9)",
            ]
        ),
        "\n".join(
            [
                "Traceback (most recent call last):",
                '  File "<stdin>", line 8, in main',
                '  File "/usr/local/lib/python3.12/dist-packages/websockets/asyncio/client.py", line 590, in __aenter__',
                "    return await self",
                "           ^^^^^^^^^^",
                '  File "/usr/lib/python3.12/asyncio/base_events.py", line 1130, in create_connection',
                "    raise OSError('Multiple exceptions: {}'.format(",
                "OSError: Multiple exceptions: [Errno 111] Connect call failed ('::1', 9, 0, 0), [Errno 111] Connect call failed ('127.0.0.1', 9)",
            ]
        ),
    ],
)
def test_loopback_connection_refused_tracebacks_skip_instead_of_failing(stderr: str) -> None:
    with pytest.raises(pytest.skip.Exception) as skipped:
        assert_smoke_client_ok(
            _client(stderr=stderr, returncode=1),
            "QA guard",
            "Uvicorn running on http://127.0.0.1:8021",
        )

    assert LOOPBACK_REFUSED_SKIP_REASON in str(skipped.value)


@pytest.mark.parametrize(
    "filename",
    [
        "test_ws_attach_real_server.py",
        "test_smoke_agenda_real_server.py",
    ],
)
def test_sister_real_server_tests_share_client_result_helper(filename: str) -> None:
    source = (HERE / filename).read_text(encoding="utf-8")

    assert "from _real_server_client import" in source
    assert "assert_smoke_client_ok(" in source
    assert "client.returncode" not in source


@pytest.mark.parametrize(
    "filename",
    [
        "smoke_ws_attach_real_server.py",
        "smoke_agenda_real_server.py",
    ],
)
def test_loopback_smoke_clients_do_not_use_host_proxy_env(filename: str) -> None:
    source = (HERE / filename).read_text(encoding="utf-8")

    assert "trust_env=False" in source
    assert "from _loopback_clients import loopback_websocket_connect" in source
    assert "websockets.connect" not in source
