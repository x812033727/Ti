"""Stage 4 外部專案的部署健康＋revision 黑盒探針。

只允許公開 HTTPS/443，解析後將 curl 釘在已驗證的公開 IP，不追蹤 redirect，
避免 admin 政策欄位被誤用為 SSRF/DNS rebinding 通道。探針回應必須是有界 JSON，
同時滿足 healthy 欄位與這次 merge SHA，舊版本仍健康不會被假綠。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import socket
from urllib.parse import urlsplit

from . import deploy

MAX_BODY_BYTES = 1_000_000


def _field(body: object, path: str) -> object:
    value = body
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _healthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in {"ok", "healthy"})


async def _public_addresses(host: str) -> list[str]:
    def resolve() -> list[str]:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(str(row[4][0]) for row in rows))

    try:
        addresses = await asyncio.wait_for(asyncio.to_thread(resolve), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return []
    # 只要 DNS 回傳一個非公開位址就整體拒絕，避免 mixed answer 繞過。
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        return []
    addresses.sort(key=lambda value: (":" in value, value))  # 優先 IPv4，簡化 curl --resolve
    return addresses


async def _once(url: str, host: str, ip: str, timeout_s: int) -> tuple[bool, dict | None, str]:
    pinned = f"{host}:443:{f'[{ip}]' if ':' in ip else ip}"
    rc, output = await deploy._run(
        [
            "curl",
            "-sS",
            "--proto",
            "=https",
            "--max-redirs",
            "0",
            "--noproxy",
            "*",
            "--connect-timeout",
            "5",
            "--max-time",
            str(max(1, min(timeout_s, 15))),
            "--max-filesize",
            str(MAX_BODY_BYTES),
            "--resolve",
            pinned,
            "-w",
            "\n%{http_code}",
            url,
        ],
        timeout=max(5, min(timeout_s, 20)),
    )
    body, _, status = (output or "").rpartition("\n")
    if rc != 0 or status.strip() != "200":
        return False, None, f"HTTP {status.strip() or '?'}"
    if len(body.encode()) > MAX_BODY_BYTES:
        return False, None, "response_too_large"
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False, None, "invalid_json"
    if not isinstance(parsed, dict):
        return False, None, "json_object_required"
    return True, parsed, "ok"


async def verify(contract: dict, expected_revision: str) -> tuple[bool, str]:
    """輪詢到 timeout；回傳訊息不含 URL/body，可安全寫 audit/通知。"""
    url = str(contract.get("health_url") or "")
    expected = str(expected_revision or "").strip().lower()
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or parsed.port not in (None, 443):
        return False, "invalid_https_health_contract"
    if len(expected) < 7 or any(char not in "0123456789abcdef" for char in expected):
        return False, "merge_revision_missing"
    addresses = await _public_addresses(host)
    if not addresses:
        return False, "health_host_not_public_or_unresolvable"
    timeout_s = int(contract.get("timeout_s") or 300)
    interval_s = int(contract.get("poll_interval_s") or 10)
    attempts = max(1, math.ceil(timeout_s / interval_s))
    last = "not_ready"
    for attempt in range(attempts):
        ok, body, detail = await _once(url, host, addresses[0], min(interval_s, timeout_s))
        if ok and body is not None:
            health = _field(body, str(contract.get("healthy_field") or ""))
            revision = str(_field(body, str(contract.get("revision_field") or "")) or "").lower()
            if _healthy(health) and revision == expected:
                return True, "health_and_revision_verified"
            last = "healthy_revision_mismatch" if _healthy(health) else "unhealthy_response"
        else:
            last = detail
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_s)
    return False, f"deployment_health_timeout:{last}"
