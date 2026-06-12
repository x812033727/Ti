"""Expert — 包裝一個 Claude Agent SDK 客戶端，代表一位具名專家。

每位專家是獨立的 ClaudeSDKClient，維持自己的對話脈絡（記得先前討論）。speak() 會把
SDK 串流回來的訊息轉成 StudioEvent，透過注入的 broadcast callback 即時送出，並回傳這次
發言的完整文字供 Orchestrator 解析決議。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import config, events, tools
from .roles import Role, effective_tools

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


def _model_for(role: Role) -> str:
    """在建立專家時（每個 session）即時讀取設定，讓模型選擇變更可於下次討論生效。"""
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
            can_use_tool=_auto_allow_tool,
            sandbox=config.expert_sandbox_settings(),
            cwd=str(cwd),
            model=_model_for(role),
            max_turns=config.MAX_TURNS_PER_TURN,
        )
    )


async def stream_to_events(messages, session_id: str, role: Role, broadcast: Broadcast) -> str:
    """把 SDK 串流訊息翻譯成 StudioEvent，回傳整段發言文字。

    抽成模組級 async 函式以開出注入縫：messages 接任意 async 可迭代，測試餵假訊息
    即可驗證事件序列與回傳文字，無需真正的 SDK 連線。判型語義（isinstance）與 SDK
    類別來源皆與原 speak() 迴圈完全相同。
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    collected: list[str] = []
    async for msg in messages:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
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
        """送出 prompt，串流回應為事件，回傳完整文字。"""
        await self.start()
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))

        await self._client.query(prompt)
        text = await stream_to_events(
            self._client.receive_response(), self.session_id, r, broadcast
        )

        await broadcast(events.expert_status(self.session_id, r.key, "idle"))
        return text
