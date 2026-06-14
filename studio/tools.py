"""給非 Claude provider 用的工具層（OpenAI function-calling）。

Claude Agent SDK 自帶 Read/Write/Edit/Bash；其他模型沒有，所以在這裡用 OpenAI 的
function-calling 規格定義同名工具，並提供實際在 workspace cwd 上執行的 execute()。
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import config, runner
from .workspace import safe_resolve

# OpenAI function-calling 工具規格
_SPECS: dict[str, dict] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "讀取 workspace 內某個檔案的內容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相對路徑"}},
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "建立或覆寫 workspace 內的檔案",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "edit_file": {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "把檔案中的一段文字替換成另一段（old 必須唯一）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    "run_bash": {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "在 workspace 執行 shell 指令（安裝套件、執行程式、跑測試）",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    "web_fetch": {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "抓取公開網頁內容供研究參考（受網域白名單與 SSRF 防護限制，輸出截斷）",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http/https 網址"}},
                "required": ["url"],
            },
        },
    },
}

# 把 Claude 工具名對應到本層工具。WebSearch 在 OpenAI 路徑無對應本地工具（無可用搜尋 API），
# 故不映射——研究員角色在 OpenAI 路徑因此自動獲得 web_fetch（WebFetch→web_fetch），屬合理增益。
_CLAUDE_TO_LOCAL = {
    "Read": ["read_file"],
    "Write": ["write_file"],
    "Edit": ["edit_file"],
    "Bash": ["run_bash"],
    "WebFetch": ["web_fetch"],
}


# --- 工具冪等性分類（單一真實來源）-----------------------------------------
# 由 providers 層的 per-speak 去重邏輯讀取：speak() 的 retry 會把整輪工具迴圈重放，
# 已成功執行的非冪等工具會被重跑造成副作用疊加。列入此集合的工具會走去重路徑
# （同 session + 同 key 的第二次呼叫回首次結果、不重執行副作用）。
#
# 【維護者注意】新增工具時，必須評估其重放語意並決定是否納入此集合：
#   - edit_file：以「old 須唯一」做就地替換，重放時 old 已被換掉 → 第二次必失敗。非冪等。
#   - run_bash：執行任意 shell 指令，可能含 `>> append`、`git push`、`curl POST` 等，
#               重放即災難；且命令內容無法靜態判斷冪等性，一律保守歸為非冪等。
#   - write_file：覆寫語意，同 args 重跑結果相同 → 天然冪等，刻意不納管（納管後 args
#                 不同時去重也攔不住，多一道邏輯卻無實際保護；殘留風險見 #6 黑樣本）。
#   - read_file / web_fetch：唯讀，無副作用 → 不納管。
NON_IDEMPOTENT_TOOLS: frozenset[str] = frozenset({"edit_file", "run_bash"})


def is_idempotent(name: str) -> bool:
    """工具是否冪等（可安全重放）。非冪等工具須由去重層保護，不可直接重執行副作用。"""
    return name not in NON_IDEMPOTENT_TOOLS


def specs_for(allowed_claude_tools: list[str]) -> list[dict]:
    """依角色的 Claude 工具清單，回傳對應的 OpenAI 工具規格（read_file 一律提供）。"""
    names = {"read_file"}
    for t in allowed_claude_tools:
        for local in _CLAUDE_TO_LOCAL.get(t, []):
            names.add(local)
    return [_SPECS[n] for n in _SPECS if n in names]


def _safe_path(cwd: Path, rel: str, *, must_exist: bool = True) -> Path | None:
    """薄包裝 workspace.safe_resolve；維持單一 containment 真實來源。

    讀取/編輯預設 must_exist=True；write_file 傳 False，避免尚未存在的新檔被誤擋。
    """
    return safe_resolve(Path(cwd), rel, must_exist=must_exist)


# --- 研究抓取（web_fetch）：SSRF／網域白名單管控 -----------------------------
# 政策的單一真實來源：research_url_check 同時供 OpenAI 路徑（execute web_fetch）與
# Claude 路徑（experts._auto_allow_tool 攔 WebFetch）共用，確保兩條路徑施加相同管控。


def _ip_block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """位址是否屬「不可抓取」範圍（私網/loopback/link-local/reserved 等）；可抓回 None。"""
    mapped = getattr(ip, "ipv4_mapped", None)  # 還原 ::ffff:a.b.c.d 再判，杜絕繞過
    if mapped is not None:
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    ):
        return f"目標位址非公開網路: {ip}"
    return None


def research_url_check(url: str) -> str | None:
    """研究抓取的 URL 政策檢查。None=放行；str=拒絕原因。

    1. scheme 限 http/https；
    2. hostname 為 IP 字面值時，私網/loopback/link-local/reserved/unspecified/multicast 一律拒（SSRF）；
    3. 網域白名單非空時，hostname 須等於白名單項或為其子網域；空白名單＝公網全放（仍擋私網 IP）。

    DNS 名稱解析後的位址檢查在實際連線前另做（見 _resolved_addr_reason），兩段式防護。
    DNS rebinding（檢查與連線各解析一次）無法完全防禦，屬已知接受風險。
    """
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return "URL 無法解析"
    if parsed.scheme not in ("http", "https"):
        return f"不支援的 scheme: {parsed.scheme or '(空)'}（僅允許 http/https）"
    host = (parsed.hostname or "").strip()
    if not host:
        return "URL 缺少 hostname"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # 非 IP 字面值（網域名）：此處不擋，留給 DNS 解析後檢查
    if ip is not None:
        reason = _ip_block_reason(ip)
        if reason:
            return reason
    domains = config.RESEARCH_ALLOWED_DOMAINS
    if domains:
        h = host.lower()
        if not any(h == d or h.endswith("." + d) for d in domains):
            return f"網域不在白名單: {host}"
    return None


def _resolved_addr_reason(url: str) -> str | None:
    """把 hostname 經 DNS 解析成位址後逐一過 _ip_block_reason；任一被擋即回原因。"""
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return f"DNS 解析失敗: {host}"
    for info in infos:
        addr = info[4][0].split("%", 1)[0]  # 剝 IPv6 zone
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reason = _ip_block_reason(ip)
        if reason:
            return reason
    return None


def _strip_html(html: str) -> str:
    """輕量剝 HTML：去 script/style 與標籤、壓縮空白（不引入解析依賴）。"""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


async def _http_get(url: str, timeout: float):
    """實際 HTTP GET（注入縫：測試 monkeypatch 此函式餵假回應，免真連線）。

    刻意關閉自動 redirect：由 _research_fetch 逐跳手動跟隨並對每一跳重跑完整管控。
    """
    import httpx

    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        return await client.get(url)


async def _research_fetch(url: str) -> str:
    """抓取網頁供研究：逐跳跟隨 redirect、每跳重驗管控、截斷輸出；任何錯誤回降級訊息（永不 raise）。"""
    reason = research_url_check(url)
    if reason:
        return f"錯誤：研究抓取被拒（{reason}），請以既有知識續行"
    current = url
    try:
        for _ in range(6):  # 原始 1 次 + 最多 5 跳 redirect
            reason = research_url_check(current)
            if reason:
                return f"錯誤：研究抓取被拒（{reason}），請以既有知識續行"
            dns_reason = _resolved_addr_reason(current)
            if dns_reason:
                return f"錯誤：研究抓取被拒（{dns_reason}），請以既有知識續行"
            resp = await _http_get(current, config.RESEARCH_FETCH_TIMEOUT)
            status = resp.status_code
            if status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if not loc:
                    return "錯誤：研究抓取失敗（redirect 無 location），請以既有知識續行"
                current = urljoin(current, loc)  # 解析相對 location
                continue
            ctype = resp.headers.get("content-type", "")
            body = resp.text or ""
            if "html" in ctype.lower():
                body = _strip_html(body)
            body = body.strip()
            cap = config.RESEARCH_FETCH_MAX_CHARS
            if len(body) > cap:
                body = body[:cap] + "\n…（已截斷）"
            return f"[HTTP {status}] {current}\n{body}"
        return "錯誤：研究抓取失敗（redirect 次數過多），請以既有知識續行"
    except Exception as exc:  # noqa: BLE001
        return f"錯誤：研究抓取失敗（{type(exc).__name__}），請以既有知識續行"


async def execute(name: str, args: dict, cwd: Path) -> str:
    """執行一個工具呼叫，回傳給模型的文字結果。"""
    cwd = Path(cwd)
    try:
        if name == "read_file":
            target = _safe_path(cwd, args.get("path", ""))
            if not target or not target.is_file():
                return f"錯誤：找不到 {args.get('path')}"
            return target.read_text(encoding="utf-8", errors="replace")

        if name == "write_file":
            # 寫新檔：目標可能尚未存在，必須 must_exist=False 否則一律被擋。
            target = _safe_path(cwd, args.get("path", ""), must_exist=False)
            if not target:
                return "錯誤：路徑超出 workspace"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.get("content", ""), encoding="utf-8")
            return f"已寫入 {args.get('path')}"

        if name == "edit_file":
            target = _safe_path(cwd, args.get("path", ""))
            if not target or not target.is_file():
                return f"錯誤：找不到 {args.get('path')}"
            text = target.read_text(encoding="utf-8")
            old = args.get("old", "")
            if text.count(old) != 1:
                return f"錯誤：old 在檔案中出現 {text.count(old)} 次，需唯一"
            target.write_text(text.replace(old, args.get("new", "")), encoding="utf-8")
            return f"已修改 {args.get('path')}"

        if name == "run_bash":
            # 刻意保留 shell：run_bash 工具的本質就是執行呼叫端給定的任意 bash 指令
            # （可能含 pipe / && / 重導向 / glob），必須經 /bin/sh 解析，無法 argv 化。
            result = await runner.run_command(cwd, args.get("command", ""))  # nosec B602
            return f"exit={result.exit_code}\n{result.output}"

        if name == "web_fetch":
            return await _research_fetch(str(args.get("url", "")))

        return f"錯誤：未知工具 {name}"
    except Exception as exc:  # noqa: BLE001
        return f"工具執行錯誤：{type(exc).__name__}: {exc}"


def summarize(name: str, args: dict) -> str:
    """給 UI 顯示的一行摘要。"""
    if name in ("read_file", "write_file", "edit_file"):
        verb = {"read_file": "讀取", "write_file": "寫入", "edit_file": "修改"}[name]
        return f"{verb} {args.get('path', '')}"
    if name == "run_bash":
        return "執行: " + (args.get("command", "")[:120])
    if name == "web_fetch":
        return "網路抓取 " + (args.get("url", "")[:120])
    return name


def parse_args(raw: str | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
