"""QA checks for PR #638 netutil IPv6 bracket-tail handling.

Run with:
    python3 qa_verify_pr638_netutil.py
"""

from __future__ import annotations

import importlib.util
import ipaddress
import sys
import types


if importlib.util.find_spec("fastapi") is None:
    fastapi_stub = types.ModuleType("fastapi")

    class Request:
        pass

    class WebSocket:
        pass

    fastapi_stub.Request = Request
    fastapi_stub.WebSocket = WebSocket
    sys.modules["fastapi"] = fastapi_stub


from studio import netutil


def assert_parse(segment: str, expected: str | None) -> None:
    actual = netutil._parse_ip(segment)
    if expected is None:
        assert actual is None, f"{segment!r}: expected None, got {actual!r}"
        return
    assert actual == ipaddress.ip_address(expected), (
        f"{segment!r}: expected {expected}, got {actual!r}"
    )


def main() -> None:
    cases: list[tuple[str, str | None]] = [
        ("[::1]junk", None),
        ("[::1] junk", None),
        ("[::1", None),
        ("[::1]", "::1"),
        ("[::1]:8080", "::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("203.0.113.9:5678", "203.0.113.9"),
        ("fe80::1%eth0", "fe80::1"),
    ]
    for segment, expected in cases:
        assert_parse(segment, expected)
    print(f"qa_verify_pr638_netutil: {len(cases)} cases passed")


if __name__ == "__main__":
    main()
