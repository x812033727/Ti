"""給非 Claude provider 用的工具層（OpenAI function-calling）。

Claude Agent SDK 自帶 Read/Write/Edit/Bash；其他模型沒有，所以在這裡用 OpenAI 的
function-calling 規格定義同名工具，並提供實際在 workspace cwd 上執行的 execute()。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Any
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
# 【將由】providers 層的 per-speak 去重邏輯讀取（待實作，見任務 #3/#4）：speak() 的 retry
# 會把整輪工具迴圈重放，已成功執行的非冪等工具會被重跑造成副作用疊加。去重層接上後，
# 列入此集合的工具會走去重路徑（同 session + 同 key 的第二次呼叫回首次結果、不重執行副作用）。
# 目前本檔僅定義「分類標記」，providers.py 尚未引用——現行執行行為與改動前完全一致。
#
# 【預設策略】白名單式 fail-open：不在此集合的工具一律視為冪等（is_idempotent 回 True）。
# 取捨——新增工具若漏評估會「靜默允許重放」而非「靜默擋住」；選 fail-open 是因為錯誤地
# 去重一個其實非冪等的工具（漏副作用）比錯誤地放行一個冪等工具（多跑一次無害重放）後果更壞，
# 故預設不納管、由維護者顯式加入集合。新增工具務必依下列語意評估是否納入：
#   - edit_file：以「old 須唯一」做就地替換，重放時 old 已被換掉 → 第二次必失敗。非冪等。
#   - run_bash：執行任意 shell 指令，可能含 `>> append`、`git push`、`curl POST` 等，
#               重放即災難；且命令內容無法靜態判斷冪等性，一律保守歸為非冪等。
#   - write_file：覆寫語意，同 args 重跑結果相同 → 天然冪等，刻意不納管（納管後 args
#                 不同時去重也攔不住，多一道邏輯卻無實際保護；殘留風險見 #6 黑樣本）。
#   - read_file / web_fetch：唯讀，無副作用 → 不納管。
NON_IDEMPOTENT_TOOLS: frozenset[str] = frozenset({"edit_file", "run_bash"})


def is_idempotent(name: str) -> bool:
    """工具是否冪等（可安全重放）。非冪等工具須由去重層保護，不可直接重執行副作用。

    預設策略為白名單式 fail-open：不在 ``NON_IDEMPOTENT_TOOLS`` 內的工具（含未知工具）
    一律回 True（視為冪等）。理由見 ``NON_IDEMPOTENT_TOOLS`` 上方註解。
    """
    return name not in NON_IDEMPOTENT_TOOLS


# --- session 內去重快取結構與 key 推導（任務 #2）---------------------------
# 【key 不依賴呼叫端 tc.id】OpenAI 重放會重新生成新的 tool_call id，用 tc.id 當 key 在
# 重放時必 miss、去重直接失效。改由「工具名 + 已解析參數（+ attempt 內出現序號）」推導：
# retry 重放同一輪工具迴圈時，tool_name 與 args 內容不變 → 同 key 命中。
def dedup_key(tool_name: str, args: dict) -> str:
    """從工具名與**已解析的 args dict** 推導去重 base key（不依賴 tc.id）。

    這是「內容指紋」——僅辨識「哪個工具、什麼參數」，不含呼叫序號。實際入快取的 key 由
    ``DedupCache.key_for`` 在此基礎上再附 attempt 內出現序號（見該類說明）；單獨使用本
    函式僅在測試辨識內容相等性時有意義。

    ``args`` 必須是 ``tools.parse_args`` 之後的 dict，不可傳 ``tc.function.arguments``
    原始 JSON 字串——後者序列化順序不穩定會讓 ``sort_keys`` 失效、同參數產生假 miss。
    """
    # sort_keys=True 確保鍵序無關，同一組 args 永遠得到同一 digest。
    digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    # [:16]＝64 bits：per-speak 工具呼叫數極小（< 100），碰撞機率 < 5×10⁻¹⁸，足夠。
    return f"{tool_name}:{digest}"


class DedupCache:
    """單次 ``speak()`` 內的去重快取（任務 #2）。純記憶體、無外部相依、不落檔、無 TTL。

    【scope】＝**單次 speak（非整個 session）**：容器由 ``OpenAIExpert`` 持有、`speak()`
    入口換上新實例（見 #4 接入），天然提供 session/speak 隔離。措辭澄清——標題寫「session
    內」，實作 scope 更窄（per-speak），功能上更安全。

    【兩層狀態、不同生命週期】
    - ``_results``：完整 key → 首次成功結果。**跨 attempt 保留**，retry 重放命中即回首次
      結果、不重執行副作用。值型別實際為 str（execute 回傳），標注從嚴用 Any。
    - ``_seen``：base key → 該 attempt 內已出現次數。**每個 attempt 由 ``new_attempt()``
      重置**，用來區分「retry 重放同一呼叫」與「同一 attempt 內 LLM 合法地下了多次相同
      args 的呼叫」。

    【為何必須有 occurrence 對齊——這是純 args hash 的正確性盲點】
    若 key 只含 ``tool_name+args``，同一 attempt 內 LLM 連下兩次 ``echo x >> log`` 會在
    第二次誤命中、只 append 一行（副作用「少跑」、靜默資料遺失，方向比「多跑」嚴重一個
    量級且在零-retry 正常路徑就觸發）。納入 attempt 內出現序號後：零-retry 路徑的合法重複
    各得不同 key（都執行）；只有「跨 attempt、同位置」的重放才命中——這才是去重要擋的對象。

    【重放對齊的前提】retry 的 ``_attempt`` 從頭重跑整輪工具迴圈，相同呼叫序列下第 N 次
    相同 (tool, args) 仍對齊到同一 ``#N``。此前提與純 args hash 相同（皆假設重放給相同
    args）；occurrence 只在「合法重複」情境嚴格更好，不會更差。
    """

    def __init__(self) -> None:
        self._results: dict[str, Any] = {}
        self._seen: dict[str, int] = {}

    def new_attempt(self) -> None:
        """每次 ``_attempt`` 開始時呼叫：重置 attempt 內出現計數，保留跨 attempt 的結果快取。"""
        self._seen = {}

    def key_for(self, tool_name: str, args: dict) -> str:
        """推導本次呼叫的完整去重 key 並遞增 attempt 內出現序號（命中與否都遞增以維持對齊）。

        同一 attempt 內第 N 次相同 (tool_name, args) → 後綴 ``#N`` 不同 → 不同 key；
        重放（``new_attempt()`` 後重跑同序列）時第 N 次仍對齊同一 ``#N`` → 命中。
        """
        base = dedup_key(tool_name, args)
        n = self._seen.get(base, 0)
        self._seen[base] = n + 1
        return f"{base}#{n}"

    def get(self, key: str) -> Any | None:
        """回傳已快取結果；未命中回 None。"""
        return self._results.get(key)

    def put(self, key: str, result: Any) -> None:
        """寫入首次成功結果。呼叫端須在副作用成功之後才寫（失敗不寫，防假命中，見 #3）。"""
        self._results[key] = result


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


# --- 去重感知的 execute（任務 #3）-------------------------------------------
# execute() 永不 raise，失敗以「錯誤字串」回傳。寫快取必須在副作用成功之後，否則
# 失敗結果被快取會造成「假命中」——重放跳過副作用卻回傳舊錯誤，把問題靜默遮蔽（架構決策）。
_ERROR_PREFIXES = ("錯誤：", "工具執行錯誤：")


def _is_error_result(result: Any) -> bool:
    """判斷 execute 回傳是否代表「副作用未成功」——這類結果不入快取（防假命中）。

    execute 的所有失敗路徑（找不到檔案、路徑越界、edit old 不唯一、未知工具、外層
    例外兜底）一律以 ``_ERROR_PREFIXES`` 開頭。

    注意 ``run_bash`` 的 ``exit=N\\n...`` 即使 N≠0 也**不**算失敗：指令已實際執行、副作用
    已發生，屬成功路徑須入快取，否則重放會把同一指令再跑一次（正是去重要擋的災難）。
    僅當 run_bash 連啟動都失敗（外層 except → 「工具執行錯誤：」）才不快取、容許重試。
    """
    return isinstance(result, str) and result.startswith(_ERROR_PREFIXES)


async def execute_deduped(name: str, args: dict, cwd: Path, cache: DedupCache | None) -> str:
    """去重感知的 ``execute``：非冪等工具經 per-speak cache 防 retry 重放重執行副作用。

    - ``cache is None`` 或冪等工具（``is_idempotent`` True，含 read_file/write_file/
      web_fetch/未知工具）：直接走 ``execute``，不碰快取，行為與接入前完全一致（驗收 #3）。
    - 非冪等工具（``NON_IDEMPOTENT_TOOLS``）：以 ``cache.key_for`` 推導對齊「attempt 內出現
      序號」的 key——
        * 命中 → 回首次成功結果、**不重執行底層副作用**（驗收 #2）；
        * 未命中 → 執行，且**僅在副作用成功後**寫快取（失敗不寫，驗收 #5）。

    ``key_for`` 只對非冪等工具呼叫，故其 attempt 內出現序號僅在非冪等呼叫間遞增；重放時
    整輪迴圈重跑、非冪等呼叫序列相同 → 同位置對齊同一 key（冪等工具夾在中間無害重放，
    不影響對齊）。
    """
    if cache is None or is_idempotent(name):
        return await execute(name, args, cwd)
    key = cache.key_for(name, args)
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = await execute(name, args, cwd)
    if not _is_error_result(result):
        cache.put(key, result)  # 副作用成功後才寫，防假命中
    return result


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
