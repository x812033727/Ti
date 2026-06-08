"""LLM provider 抽象與工廠。

預設 Claude（走 Agent SDK，自帶工具）。也支援 OpenAI 相容介面（含本地模型，如 Ollama /
LM Studio 透過 OPENAI_BASE_URL），用 tools.py 的 function-calling 工具迴圈讓模型也能「自己 coding」。

所有 backend 都符合 orchestrator 的 ExpertLike 介面：`speak(prompt, broadcast) -> str` 與 `stop()`。
"""

from __future__ import annotations

from pathlib import Path

from . import config, events, tools
from .roles import Role


def openai_model_for(role: Role) -> str:
    return config.OPENAI_MODEL_LEAD if role.key in config.LEAD_ROLES else config.OPENAI_MODEL_FAST


class OpenAIExpert:
    """以 OpenAI 相容 chat completions + function-calling 工具迴圈驅動的專家。

    chat 是注入的 async callable(messages, tools, model) -> response，方便測試替換。
    """

    def __init__(self, role: Role, session_id: str, cwd: Path, chat, model: str):
        self.role = role
        self.session_id = session_id
        self.cwd = cwd
        self._chat = chat
        self._model = model
        self._tools = tools.specs_for(role.allowed_tools)
        self._messages: list[dict] = [{"role": "system", "content": role.system_prompt}]

    async def speak(self, prompt: str, broadcast) -> str:
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        self._messages.append({"role": "user", "content": prompt})
        collected: list[str] = []

        for _ in range(config.OPENAI_MAX_STEPS):
            resp = await self._chat(self._messages, self._tools, self._model)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            self._messages.append(_assistant_dict(msg, tool_calls))

            if tool_calls:
                await broadcast(events.expert_status(self.session_id, r.key, "working"))
                for tc in tool_calls:
                    name = tc.function.name
                    args = tools.parse_args(tc.function.arguments)
                    result = await tools.execute(name, args, self.cwd)
                    await broadcast(
                        events.tool_use(self.session_id, r.key, name, tools.summarize(name, args))
                    )
                    self._messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result}
                    )
                continue

            text = (msg.content or "").strip()
            if text:
                collected.append(text)
                await broadcast(
                    events.expert_message(self.session_id, r.key, r.name, r.avatar, text)
                )
            break

        await broadcast(events.expert_status(self.session_id, r.key, "idle"))
        return "\n".join(collected)

    async def stop(self) -> None:
        pass


def _assistant_dict(msg, tool_calls) -> dict:
    d: dict = {"role": "assistant", "content": getattr(msg, "content", None) or ""}
    if tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ]
    return d


async def _openai_chat(messages, tools_, model):
    import openai

    client = openai.AsyncOpenAI(
        api_key=config.OPENAI_API_KEY or "sk-none",
        base_url=config.OPENAI_BASE_URL or None,
    )
    return await client.chat.completions.create(
        model=model, messages=messages, tools=tools_ or None
    )


def make_expert(role: Role, session_id: str, cwd: Path):
    """依設定的 provider 建立一位專家。"""
    if config.PROVIDER == "openai":
        return OpenAIExpert(role, session_id, cwd, chat=_openai_chat, model=openai_model_for(role))
    # 預設：Claude Agent SDK（延後 import，避免無 SDK 時就失敗）
    from .experts import Expert

    return Expert(role, session_id, cwd)
