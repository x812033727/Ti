"""Expert — 包裝一個 Claude Agent SDK 客戶端，代表一位具名專家。

每位專家是獨立的 ClaudeSDKClient，維持自己的對話脈絡（記得先前討論）。speak() 會把
SDK 串流回來的訊息轉成 StudioEvent，透過注入的 broadcast callback 即時送出，並回傳這次
發言的完整文字供 Orchestrator 解析決議。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from . import config, events
from .roles import Role

Broadcast = Callable[[events.StudioEvent], Awaitable[None]]

# PM 與高級工程師用主力（推理強）模型，其餘用快速模型。
_LEAD_ROLES = {"pm", "senior"}


async def _auto_allow_tool(
    tool_name: str, tool_input: dict, context: ToolPermissionContext
) -> PermissionResultAllow:
    """自動核可專家請求的工具。

    工作室是無人值守後端，沒有真人能回答 SDK 的權限詢問；改用此回呼放行，取代
    bypassPermissions（後者會傳 --dangerously-skip-permissions，在 root 服務下被
    Claude CLI 拒絕）。每位專家可用的工具已由 role.allowed_tools 白名單限制，且各自
    跑在獨立 workspace（cwd）內。
    """
    return PermissionResultAllow()


def _model_for(role: Role) -> str:
    """在建立專家時（每個 session）即時讀取設定，讓模型選擇變更可於下次討論生效。"""
    return config.MODEL_LEAD if role.key in _LEAD_ROLES else config.MODEL_FAST


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


class Expert:
    def __init__(self, role: Role, session_id: str, cwd: Path):
        self.role = role
        self.session_id = session_id
        self._client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                system_prompt=role.system_prompt,
                allowed_tools=role.allowed_tools,
                permission_mode=role.permission_mode,
                can_use_tool=_auto_allow_tool,
                cwd=str(cwd),
                model=_model_for(role),
                max_turns=config.MAX_TURNS_PER_TURN,
            )
        )
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

        collected: list[str] = []
        await self._client.query(prompt)
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text = block.text.strip()
                        if text:
                            collected.append(text)
                            await broadcast(
                                events.expert_message(
                                    self.session_id, r.key, r.name, r.avatar, text
                                )
                            )
                    elif isinstance(block, ToolUseBlock):
                        await broadcast(events.expert_status(self.session_id, r.key, "working"))
                        await broadcast(
                            events.tool_use(
                                self.session_id,
                                r.key,
                                block.name,
                                _summarize_tool(block.name, block.input or {}),
                            )
                        )
            elif isinstance(msg, ResultMessage):
                break

        await broadcast(events.expert_status(self.session_id, r.key, "idle"))
        return "\n".join(collected)
