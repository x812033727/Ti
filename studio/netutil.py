"""來源 IP 判斷的純函式工具：在反向代理後解析真實 client IP。

設計重點（opt-in、fail-safe）：
- `TI_TRUST_PROXY` 關閉（預設）時：完全忽略 X-Forwarded-For，只認 socket peer，向後相容。
- 開啟後：僅當 socket peer 屬於 `TI_TRUSTED_PROXIES` 內的受信代理，才解析 XFF；
  解析採「由右往左跳過受信代理、取最右第一個非受信位址」為真實 client，嚴禁採信最左值。
- 任何無法乾淨判定的情況一律 fail-safe 回退 socket peer，絕不回傳無法解析的原始字串。

純函式：吃 `Request | WebSocket`（沿用 auth.py 型別慣例，順帶支援 WebSocket），
不改 `scope`、不掛 middleware、不污染 auth.py。
"""

from __future__ import annotations

import ipaddress

from fastapi import Request, WebSocket

from . import config

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse_ip(segment: str) -> _IPAddress | None:
    """把單一 XFF 段（或 peer host）剝離 port 與 IPv6 zone 後解析為 ip_address。

    可處理：`1.2.3.4`、`1.2.3.4:5678`、`[::1]:port`、`[2001:db8::1]`、
    `::1`、`fe80::1%eth0`。無法乾淨解析者回 None（呼叫端視為斷鏈止點）。
    """
    s = segment.strip()
    if not s:
        return None

    if s.startswith("["):
        # 帶中括號的 IPv6，可能尾隨 :port —— 取中括號內內容。
        end = s.find("]")
        if end == -1:
            return None
        host = s[1:end]
    elif s.count(":") == 1:
        # 剛好一個冒號 → 視為 IPv4:port。
        host = s.split(":", 1)[0]
    else:
        # 無冒號（裸 IPv4）或多個冒號（裸 IPv6，無 port）。
        host = s

    # 剝離 IPv6 zone id（如 fe80::1%eth0）。
    host = host.split("%", 1)[0].strip()
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_trusted(ip: _IPAddress) -> bool:
    """ip 是否落在任一受信代理網段內（版本不符的網段會自動回 False）。"""
    return any(ip in net for net in config.trusted_proxies())


def _xff_segments(scope: Request | WebSocket) -> list[str]:
    """彙整所有同名 X-Forwarded-For header（ASGI 可能多筆），逗號合併後切分。"""
    raw = scope.headers.getlist("x-forwarded-for")
    segments: list[str] = []
    for value in raw:
        # 過濾純空段（尾隨逗號/連續逗號產生的 ""/空白）：空段是分隔副產物、非真實跳點，
        # 不可當斷鏈止點，否則尾逗號的 XFF 會讓外部來源誤回退成 loopback peer。
        # 垃圾值（無法解析的非空段）仍由 client_ip 當硬止點處理，兩者語意不同。
        segments.extend(part for part in value.split(",") if part.strip())
    return segments


def client_ip(scope: Request | WebSocket) -> str | None:
    """回傳請求的真實來源 IP 字串；無法判定時回 None。

    - socket peer 不可知（`scope.client is None`）→ None。
    - `TI_TRUST_PROXY` 關閉 → 一律回 socket peer、完全忽略 XFF。
    - 開啟但 socket peer 非受信代理 → 仍回 socket peer（不採信 XFF）。
    - 開啟且 peer 屬受信代理 → 由右往左掃 XFF，跳過受信代理位址，
      取最右第一個「可解析且非受信」位址；遇無法解析段或全鏈皆受信 → 回退 socket peer。
    """
    client = scope.client
    if client is None:
        return None
    peer_host = client.host

    if not config.trust_proxy_enabled():
        return peer_host

    peer_ip = _parse_ip(peer_host)
    # peer 無法解析或不在受信清單 → 不採信 XFF。
    if peer_ip is None or not _is_trusted(peer_ip):
        return peer_host

    # peer 是受信代理：由右往左掃 XFF 找最右非受信位址。
    for segment in reversed(_xff_segments(scope)):
        ip = _parse_ip(segment)
        if ip is None:
            # 斷鏈止點：fail-safe 回退 socket peer。
            return peer_host
        if _is_trusted(ip):
            continue
        return str(ip)

    # XFF 為空或全鏈皆受信代理 → 回退 socket peer。
    return peer_host


def is_loopback(scope: Request | WebSocket) -> bool:
    """請求真實來源是否為本機（loopback）。fail-closed：無法判定一律回 False。

    建立在 client_ip() 之上，用 `ipaddress.is_loopback` 判斷（涵蓋 127.0.0.0/8 與 ::1），
    全程禁止 `== "127.0.0.1"` 字串比對。對 IPv4-mapped IPv6（如 ::ffff:127.0.0.1）
    先還原為 IPv4 再判，避免漏判被當繞過漏洞。全程包 try/except，絕不誤判為 loopback。
    """
    try:
        ip_str = client_ip(scope)
        if ip_str is None:
            return False
        ip = ipaddress.ip_address(ip_str)
        # 還原 IPv4-mapped IPv6（::ffff:a.b.c.d）為 IPv4 再判。
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        return ip.is_loopback
    except (ValueError, TypeError):
        return False
