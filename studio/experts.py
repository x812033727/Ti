"""Expert — 包裝一個 Claude Agent SDK 客戶端，代表一位具名專家。

每位專家是獨立的 ClaudeSDKClient，維持自己的對話脈絡（記得先前討論）。speak() 會把
SDK 串流回來的訊息轉成 StudioEvent，透過注入的 broadcast callback 即時送出，並回傳這次
發言的完整文字供 Orchestrator 解析決議。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import config, events, tools
from .roles import Role, effective_tools

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # 僅供型別檢查與註記用；執行期改在各函式內 local import，讓不需要 SDK 的
    # 輕量用途（如 _model_for、import studio.experts）在未安裝 claude-agent-sdk
    # 的環境（CI test job）也能載入。
    from claude_agent_sdk import (
        PermissionResultAllow,
        PermissionResultDeny,
        ToolPermissionContext,
    )

Broadcast = Callable[[events.StudioEvent], Awaitable[None]]


class ExpertTurnTimeout(Exception):
    """專家發言逾時（idle＝串流久無進展；hard＝總時長超過上限）。

    max_turns 限不住單一工具呼叫卡死（如前景跑常駐 server），由此例外把卡住的
    turn 標記出來，partial_text 保留逾時前已收到的文字供失敗回饋使用。
    """

    def __init__(self, reason: str, partial_text: str):
        super().__init__(reason)
        self.reason = reason  # "idle" | "hard"
        self.partial_text = partial_text


class ExpertRateLimited(Exception):
    """偵測到 429／rate_limit_error——交由 speak 層做有限次 retry-after 退避重試。

    snippet＝命中錯誤的原文片段（供 log）；partial_text＝命中前已收到的合法文字。
    retry_after＝從錯誤文字／例外解析到的建議等待秒數（無則 None，改走指數退避）。
    """

    def __init__(self, retry_after: float | None, snippet: str, partial_text: str = ""):
        super().__init__(snippet)
        self.retry_after = retry_after
        self.snippet = snippet
        self.partial_text = partial_text


class ExpertAPIError(Exception):
    """偵測到非限流的 API 錯誤文字（如 overloaded_error）——視為該輪失敗走 fallback。

    與限流分屬兩條獨立失敗路徑：不重試，直接回傳不含核可關鍵詞的系統說明文字。
    """

    def __init__(self, kind: str, snippet: str, partial_text: str = ""):
        super().__init__(snippet)
        self.kind = kind
        self.snippet = snippet
        self.partial_text = partial_text


# 失敗 fallback 文字的穩定標記子字串——單一事實來源，供下游（如冒煙報告）以純消費端
# 方式從 transcript 計數 429／SDK 錯誤文字命中，避免兩端字串各寫一份而漂移。
RATE_LIMIT_FALLBACK_MARKER = "因 API 限流（429）"
API_ERROR_FALLBACK_MARKER = "發言收到 API 錯誤"

# Anthropic 錯誤封包形如 {"type":"error","error":{"type":"rate_limit_error",...}}。
# 錨定「JSON key:value 的錯誤型別 token」而非裸關鍵字，避免專家正常引用「rate limit／
# error」字樣被誤殺（架構決策：禁用寬鬆關鍵字）。
_API_ERR_RE = re.compile(
    r'"type"\s*:\s*"(?P<kind>rate_limit_error|overloaded_error|api_error|'
    r"authentication_error|permission_error|not_found_error|request_too_large|"
    r'invalid_request_error|billing_error|timeout_error)"'
)
# 僅在 status/error code/HTTP 等明確前綴後出現的數字才視為狀態碼（裸數字不算）。
_STATUS_RE = re.compile(
    r"(?:status(?:\s*code)?|error\s*code|HTTP)\D{0,8}(?P<code>4\d\d|5\d\d)", re.I
)
# retry-after：header 或 JSON 欄位皆容忍（秒）。
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[\"'\s:=]+(?P<sec>\d+(?:\.\d+)?)", re.I)
# 視為 API 錯誤（走 fallback）的狀態碼；其餘僅當有錯誤型別 token 才算。
_API_ERROR_CODES = {"400", "401", "403", "404", "413", "500", "502", "503", "529"}


def _parse_retry_after(text: str) -> float | None:
    m = _RETRY_AFTER_RE.search(text or "")
    return float(m.group("sec")) if m else None


def _classify_api_text(text: str) -> tuple[str, object] | None:
    """判斷一段文字是否為 API 錯誤封包。

    回傳 ("rate_limit", retry_after|None) ／ ("api_error", kind) ／ None。
    rate_limit 條件：型別 token 為 rate_limit_error，或明確前綴後的狀態碼為 429。
    """
    if not text:
        return None
    m = _API_ERR_RE.search(text)
    kind = m.group("kind") if m else None
    sm = _STATUS_RE.search(text)
    code = sm.group("code") if sm else None
    if kind == "rate_limit_error" or code == "429":
        return ("rate_limit", _parse_retry_after(text))
    if kind:
        return ("api_error", kind)
    if code and code in _API_ERROR_CODES:
        return ("api_error", f"HTTP {code}")
    return None


def _classify_failure(exc: Exception) -> tuple[str, float | None, str, str]:
    """把 stream/query 拋出的例外歸類為 rate_limit／api_error／unknown。

    回傳 (kind, retry_after, snippet, partial_text)。涵蓋兩種 SDK 失敗形態：
    (a) 本模組從錯誤文字主動拋出的 ExpertRateLimited／ExpertAPIError；
    (b) SDK 例外型 429（issue #812）——以 str(exc) 套同一錨定樣式辨識。
    unknown 不吞，由呼叫端 re-raise，不掩蓋真正的程式錯誤。
    """
    if isinstance(exc, ExpertRateLimited):
        return ("rate_limit", exc.retry_after, exc.snippet, exc.partial_text)
    if isinstance(exc, ExpertAPIError):
        return ("api_error", None, exc.snippet, exc.partial_text)
    hit = _classify_api_text(str(exc))
    if hit and hit[0] == "rate_limit":
        return ("rate_limit", hit[1], str(exc)[:300], "")
    if hit and hit[0] == "api_error":
        return ("api_error", None, str(exc)[:300], "")
    return ("unknown", None, "", "")


def _backoff_delay(retry_after: float | None, attempt: int) -> float:
    """退避秒數：優先採 retry-after，否則指數退避；皆夾在 cap 內。"""
    cap = config.EXPERT_RATE_LIMIT_BACKOFF_CAP
    if retry_after and retry_after > 0:
        return min(retry_after, cap)
    return min(config.EXPERT_RATE_LIMIT_BACKOFF * (2**attempt), cap)


async def _sleep(seconds: float) -> None:
    """退避等待的注入縫：測試 monkeypatch 本函式即可零實際等待並記錄延遲。"""
    if seconds > 0:
        await asyncio.sleep(seconds)


# 哪些角色用主力（強但慢）模型，由 config.LEAD_ROLES 控制（可調、可在設定頁改）。


async def _auto_allow_tool(
    tool_name: str, tool_input: dict, context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """自動核可專家請求的工具。

    工作室是無人值守後端，沒有真人能回答 SDK 的權限詢問；改用此回呼放行，取代
    bypassPermissions（後者會傳 --dangerously-skip-permissions，在 root 服務下被
    Claude CLI 拒絕）。每位專家可用的工具已由 role.allowed_tools 白名單限制，且各自
    跑在獨立 workspace（cwd）內。

    例外：WebFetch（實作中即時研究）的目標 URL 須過 tools.research_url_check 的
    SSRF／網域白名單管控——CLI 沙箱的 allowedDomains 只管 bash 網路、攔不到 WebFetch，
    故在此攔。WebSearch 無 URL 可驗、流量在 Anthropic 端，無法施加白名單（見 README）。
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    if tool_name == "WebFetch":
        reason = tools.research_url_check(str((tool_input or {}).get("url", "")))
        if reason:
            return PermissionResultDeny(message=f"研究網域管控：{reason}")
    return PermissionResultAllow()


# SDK 內建的寫檔工具（Claude provider 用它們，不經 studio 的 tools.execute／safe_resolve）。
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _path_within(cwd: Path, raw: str) -> bool:
    """raw（相對或絕對）解析後是否仍落在 cwd（含子目錄）內。解析失敗一律視為「不在內」。"""
    try:
        p = Path(raw)
        target = p if p.is_absolute() else (cwd / p)
        return target.resolve().is_relative_to(cwd.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def _make_can_use_tool(cwd: Path):
    """產生綁定該專家 cwd 的權限回呼。

    並行 lane 隔離的真正防線：Claude provider 的 SDK 內建 Write/Edit/MultiEdit/NotebookEdit
    **不經** studio 的 `tools.execute`／`safe_resolve`，其檔案寫入是否限制在 cwd 內全靠 CLI
    沙箱的 FS 邊界——而該邊界在巢狀沙箱／缺依賴時可能靜默未生效，導致 lane 專家把成果寫到
    cwd 外的兄弟目錄（主工作樹），使「並行 lane 隔離」名不副實、合併變 no-op／撞未追蹤檔。
    故在此於權限層硬擋「寫到 cwd 之外」（is_relative_to 判定，含絕對路徑與 `..` 逃逸），
    與 OpenAI／離線路徑的 safe_resolve 對齊；其餘（WebFetch 管控、預設放行）沿用既有行為。
    序列模式 cwd＝主工作目錄，專家本就在其中寫檔，不受影響。
    """
    cwd = Path(cwd)

    async def can_use_tool(tool_name, tool_input, context):
        from claude_agent_sdk import PermissionResultDeny

        if tool_name in _WRITE_TOOLS:
            data = tool_input or {}
            raw = str(
                data.get("file_path") or data.get("notebook_path") or data.get("path") or ""
            )
            if raw and not _path_within(cwd, raw):
                return PermissionResultDeny(
                    message=f"工作隔離：禁止寫入工作目錄外的路徑（{raw}）"
                )
        return await _auto_allow_tool(tool_name, tool_input, context)

    return can_use_tool


def _model_for(role: Role) -> str:
    """在建立專家時（每個 session）即時讀取設定，讓模型選擇變更可於下次討論生效。

    優先序：該角色的個別覆寫（config.ROLE_MODELS，設定面板「<角色>模型」欄位）
    → 沒覆寫（auto）就沿用 LEAD_ROLES → MODEL_LEAD/FAST 的二分法。
    """
    override = config.ROLE_MODELS.get(role.key, "")
    if override:
        return override
    return config.MODEL_LEAD if role.key in config.LEAD_ROLES else config.MODEL_FAST


def _summarize_tool(name: str, tool_input: dict) -> str:
    """把工具呼叫變成人類可讀的一行摘要，給 UI 顯示。"""
    fp = tool_input.get("file_path") or tool_input.get("path")
    if name in ("Write", "Edit", "Read") and fp:
        verb = {"Write": "寫入", "Edit": "修改", "Read": "讀取"}[name]
        return f"{verb} {Path(fp).name}"
    if name == "Bash":
        cmd = (tool_input.get("command") or "").strip().splitlines()
        return "執行: " + (cmd[0][:120] if cmd else "")
    if name in ("Grep", "Glob"):
        return f"{name}: {tool_input.get('pattern', '')}"
    return name


def _build_client(role: Role, session_id: str, cwd: Path):
    """建立該專家的 ClaudeSDKClient。

    抽成模組級函式以開出注入縫：測試可 monkeypatch 本函式回傳假 client，
    從而在未安裝 claude-agent-sdk、不連線的情況下驗證 Expert 生命週期。
    執行期內容與原 __init__ 完全相同。
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    return ClaudeSDKClient(
        options=ClaudeAgentOptions(
            system_prompt=role.system_prompt,
            allowed_tools=effective_tools(role),
            permission_mode=role.permission_mode,
            can_use_tool=_make_can_use_tool(cwd),
            sandbox=config.expert_sandbox_settings(),
            cwd=str(cwd),
            model=_model_for(role),
            max_turns=config.MAX_TURNS_PER_TURN,
        )
    )


async def stream_to_events(
    messages,
    session_id: str,
    role: Role,
    broadcast: Broadcast,
    *,
    idle_timeout: float | None = None,
    hard_timeout: float | None = None,
) -> str:
    """把 SDK 串流訊息翻譯成 StudioEvent，回傳整段發言文字。

    抽成模組級 async 函式以開出注入縫：messages 接任意 async 可迭代，測試餵假訊息
    即可驗證事件序列與回傳文字，無需真正的 SDK 連線。判型語義（isinstance）與 SDK
    類別來源皆與原 speak() 迴圈完全相同。

    watchdog：idle_timeout＝相鄰兩則訊息的間隔上限（每則訊息重置，含工具呼叫間的
    心跳，故不誤殺正常長發言）；hard_timeout＝整段串流總時長兜底。逾時拋
    ExpertTurnTimeout（帶已收到的部分文字）。兩者皆 None＝原行為，既有呼叫不受影響。
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    collected: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + hard_timeout if hard_timeout else None
    it = messages.__aiter__()
    while True:
        wait: float | None = idle_timeout or None
        reason = "idle"
        if deadline is not None:
            remaining = deadline - loop.time()
            if wait is None or remaining < wait:
                wait, reason = remaining, "hard"
        try:
            if wait is None:
                msg = await it.__anext__()
            elif wait <= 0:
                raise ExpertTurnTimeout(reason, "\n".join(collected))
            else:
                msg = await asyncio.wait_for(it.__anext__(), wait)
        except StopAsyncIteration:
            break
        except TimeoutError:
            raise ExpertTurnTimeout(reason, "\n".join(collected)) from None
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
                        # 429／SDK 錯誤文字防線：部分 API 錯誤被塞進 AssistantMessage 文字
                        # （issue #472），若當成正常發言進 transcript 會污染決議解析。命中
                        # 即在此唯一收斂點拋出，交由 speak 層退避重試（限流）或走 fallback
                        # （其它 API 錯誤），不廣播為正常訊息。
                        hit = _classify_api_text(text)
                        if hit is not None:
                            partial = "\n".join(collected)
                            if hit[0] == "rate_limit":
                                raise ExpertRateLimited(hit[1], text[:300], partial)
                            raise ExpertAPIError(str(hit[1]), text[:300], partial)
                        collected.append(text)
                        await broadcast(
                            events.expert_message(
                                session_id, role.key, role.name, role.avatar, text
                            )
                        )
                elif isinstance(block, ToolUseBlock):
                    await broadcast(events.expert_status(session_id, role.key, "working"))
                    await broadcast(
                        events.tool_use(
                            session_id,
                            role.key,
                            block.name,
                            _summarize_tool(block.name, block.input or {}),
                        )
                    )
        elif isinstance(msg, ResultMessage):
            break
    return "\n".join(collected)


class Expert:
    def __init__(self, role: Role, session_id: str, cwd: Path):
        self.role = role
        self.session_id = session_id
        self._cwd = cwd  # 逾時斷線後重建 client 需要
        self._client = _build_client(role, session_id, cwd)
        self._connected = False

    async def start(self) -> None:
        if not self._connected:
            await self._client.connect()
            self._connected = True

    async def stop(self) -> None:
        if self._connected:
            try:
                await self._client.disconnect()
            finally:
                self._connected = False

    async def speak(self, prompt: str, broadcast: Broadcast) -> str:
        """送出 prompt，串流回應為事件，回傳完整文字。

        受 config.TURN_IDLE_TIMEOUT / TURN_HARD_TIMEOUT 的發言層 watchdog 保護：
        逾時不拋例外，改回傳「【系統】逾時中止」說明文字——其中不含任何核可關鍵詞，
        QA／審查的解析自然視為未通過，走既有的失敗回饋／停滯收斂路徑，orchestrator
        無需任何改動。timeout 放在這裡而非 _speak 包裝層，使 _debate、架構決策等
        直呼 speak 的路徑同樣受保護。
        """
        await self.start()
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        text = await self._speak_with_retries(prompt, broadcast)
        await broadcast(events.expert_status(self.session_id, r.key, "idle"))
        return text

    async def _speak_with_retries(self, prompt: str, broadcast: Broadcast) -> str:
        """送 prompt 並串流；429／限流以有限次 retry-after 退避重試，其餘 API 錯誤走 fallback。

        退避迴圈包住整段 `query()`＋`stream_to_events()`（架構決策：例外型 429 可能在
        query 階段拋出，只包串流會漏接），且置於 watchdog（ExpertTurnTimeout）的更外層
        ——逾時是另一條獨立失敗路徑，不被退避吞掉。未知例外一律 re-raise，不掩蓋真錯。
        """
        r = self.role
        max_retries = max(0, config.EXPERT_RATE_LIMIT_RETRIES)
        attempt = 0
        while True:
            try:
                await self._client.query(prompt)
                return await stream_to_events(
                    self._client.receive_response(),
                    self.session_id,
                    r,
                    broadcast,
                    idle_timeout=config.TURN_IDLE_TIMEOUT or None,
                    hard_timeout=config.TURN_HARD_TIMEOUT or None,
                )
            except ExpertTurnTimeout as exc:
                return await self._abort_turn(exc, broadcast)
            except Exception as exc:
                kind, retry_after, snippet, partial = _classify_failure(exc)
                if kind == "rate_limit":
                    if attempt < max_retries:
                        delay = _backoff_delay(retry_after, attempt)
                        logger.warning(
                            "專家 %s 撞限流（429，第 %d/%d 次重試），退避 %.1fs：%s",
                            r.key,
                            attempt + 1,
                            max_retries,
                            delay,
                            snippet,
                        )
                        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
                        await _sleep(delay)
                        attempt += 1
                        continue
                    logger.warning("專家 %s 限流重試耗盡（%d 次），走 fallback", r.key, max_retries)
                    return await self._fallback_note(
                        f"【系統】發言{RATE_LIMIT_FALLBACK_MARKER}退避重試 {max_retries} 次仍失敗，本輪中止。",
                        partial,
                        broadcast,
                    )
                if kind == "api_error":
                    logger.warning("專家 %s 收到 API 錯誤文字，走 fallback：%s", r.key, snippet)
                    return await self._fallback_note(
                        f"【系統】{API_ERROR_FALLBACK_MARKER}，本輪中止。", partial, broadcast
                    )
                raise

    async def _fallback_note(self, note: str, partial_text: str, broadcast: Broadcast) -> str:
        """限流／錯誤文字失敗時的收斂：對齊逾時 fallback 語義——回傳不含核可關鍵詞的
        系統說明文字並照常廣播進 transcript，由下游既有機制視為未過。"""
        r = self.role
        if partial_text:
            note += f"\n中止前的部分輸出：\n{partial_text}"
        await broadcast(events.expert_message(self.session_id, r.key, r.name, r.avatar, note))
        return note

    async def _abort_turn(self, exc: ExpertTurnTimeout, broadcast: Broadcast) -> str:
        """逾時後回收：先溫和 interrupt 並 drain 到 turn 邊界；不行才斷線重建。

        取消串流讀取不會停掉 CLI 子行程裡卡住的工具，所以必須 interrupt；drain 到
        ResultMessage 是為了讓對話停在乾淨的 turn 邊界，否則殘留訊息會污染下一次
        receive_response()。interrupt／drain 失敗（如 Bash 卡死殺不掉）就 disconnect
        殺掉子行程並重建 client——對話脈絡歸零，但 lane 解鎖；脈絡損失由 orchestrator
        既有的 feedback／NOTES／reflexion 機制補償。
        """
        r = self.role
        kind = "閒置（串流無進展）" if exc.reason == "idle" else "總時長"
        note = f"【系統】發言逾時中止（{kind}上限）。"
        try:
            from claude_agent_sdk import ResultMessage

            await self._client.interrupt()

            async def _drain() -> None:
                async for msg in self._client.receive_response():
                    if isinstance(msg, ResultMessage):
                        return

            await asyncio.wait_for(_drain(), 30)
        except Exception:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._connected = False
            self._client = _build_client(r, self.session_id, self._cwd)
            note += "（會話無法中斷，已重建；此前脈絡遺失）"
        if exc.partial_text:
            note += f"\n逾時前的部分輸出：\n{exc.partial_text}"
        await broadcast(events.expert_message(self.session_id, r.key, r.name, r.avatar, note))
        return note
