"""LLM provider 抽象與工廠。

預設 Claude（走 Agent SDK，自帶工具）。也支援 OpenAI 相容介面（含本地模型，如 Ollama /
LM Studio 透過 OPENAI_BASE_URL），用 tools.py 的 function-calling 工具迴圈讓模型也能「自己 coding」。

所有 backend 都符合 orchestrator 的 ExpertLike 介面：`speak(prompt, broadcast) -> str` 與 `stop()`。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from . import config, events, llm_caller, tools
from .experts import _make_retry_observer as make_retry_observer, make_retry_config
from .roles import Role, effective_tools

logger = logging.getLogger(__name__)


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
        self._tools = tools.specs_for(effective_tools(role))
        self._messages: list[dict] = [{"role": "system", "content": role.system_prompt}]

    async def speak(self, prompt: str, broadcast) -> str:
        """送出 prompt，跑 function-calling 工具迴圈，回傳完整發言文字。

        整個工具迴圈打包為單一 `_attempt`，交核心 `llm_caller.run_with_retries` 控制退避，
        與 Claude 端（experts._speak_with_retries）共用同一 `make_retry_config()` 旋鈕：
        - 命中 429／5xx（限流／過載）時走有限次退避重試；非限流 API 錯誤與重試耗盡皆回退空字串
          （對齊既有 except→"" 行為），未知例外由骨幹原樣 re-raise，不掩蓋真錯。
        - idle 廣播置於 `finally`，覆蓋成功／限流耗盡／api_error／未知例外四路徑。

        retry 假設工具呼叫為冪等——429 多發生在 `_chat`（LLM 呼叫）階段，工具執行本身不觸發
        限流。寫入型／非冪等工具不在此 retry 的安全保證範圍內，須於 `tools.execute` 層自行
        防護；整輪工具迴圈被重放的機率低，但維護者須知此前提。
        """
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        cfg = make_retry_config()
        # speak 進入時快照訊息歷史；user 訊息與 collected 都搬進 _attempt，retry 時以
        # snapshot + [user_msg] 還原，確保多次嘗試不把歷史重複累加。
        snapshot = list(self._messages)
        user_msg = {"role": "user", "content": prompt}

        async def _attempt() -> str:
            self._messages[:] = snapshot + [user_msg]
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
                            events.tool_use(
                                self.session_id, r.key, name, tools.summarize(name, args)
                            )
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

            return "\n".join(collected)

        async def _on_retry(attempt: int, limit: int, delay: float, snippet: str) -> None:
            logger.warning(
                "OpenAI 專家 %s 撞限流／過載（429／5xx，第 %d/%d 次重試），退避 %.1fs：%s",
                r.key,
                attempt + 1,
                limit,
                delay,
                snippet,
            )
            await broadcast(events.expert_status(self.session_id, r.key, "thinking"))

        async def _on_rate_limit_exhausted(snippet: str, partial: str) -> str:
            logger.warning(
                "OpenAI 專家 %s 限流重試耗盡（%d 次），回退空字串：%s",
                r.key,
                cfg.max_retries,
                snippet,
            )
            return ""

        async def _on_api_error(snippet: str, partial: str) -> str:
            logger.warning("OpenAI 專家 %s 收到 API 錯誤，回退空字串：%s", r.key, snippet)
            return ""

        # 與 Claude 端 _speak_with_retries 對稱接上可觀測接點：metrics 累加退避次數／延遲，
        # observe sink 落結構化 log。兩者皆純記錄、不改控制流。
        metrics = llm_caller.RetryMetrics()
        try:
            text = await llm_caller.run_with_retries(
                _attempt,
                **cfg.as_kwargs(),
                on_retry=_on_retry,
                on_rate_limit_exhausted=_on_rate_limit_exhausted,
                on_api_error=_on_api_error,
                metrics=metrics,
                observe=make_retry_observer(r.key),
            )
            if metrics.retries or metrics.outcome not in ("success", ""):
                logger.info("OpenAI 專家 %s 發言收斂：%s", r.key, metrics.to_dict())
            return text
        finally:
            await broadcast(events.expert_status(self.session_id, r.key, "idle"))

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


async def complete_once(
    system: str,
    user: str,
    *,
    session_id: str,
    cwd: Path | None,
    timeout: float = 120.0,
) -> str:
    """單輪「system + user → 純文字」呼叫，供反思等廉價用途；永不 raise。

    走現有 provider 抽象（make_expert）：claude 用無工具的一次性 Expert、openai 用無工具 chat。
    任何例外／逾時／離線／無 cwd 一律回 ""，讓呼叫端走模板 fallback——絕不讓反思失敗拖垮主迴圈。
    SDK import 維持 lazy（在 Expert 建構時），CI 無 SDK 環境只要不實際呼叫即安全。
    """
    if cwd is None or config.OFFLINE_MODE or not config.provider_ready():
        # provider 無憑證時直接走模板 fallback：避免每次失敗輪都白等 SDK 啟動失敗
        # （無金鑰環境下可達數十秒），拖慢主迴圈與測試。
        return ""
    role = Role(
        key="oneshot",  # 不屬 LEAD_ROLES → 用 MODEL_FAST（廉價）
        name="反思",
        avatar="🪞",
        title="Reflector",
        model=config.MODEL_FAST,
        allowed_tools=[],  # 無工具：claude 不開工具、openai 工具迴圈首回合即收斂
        permission_mode="default",
        system_prompt=system,
    )

    async def _noop(_ev) -> None:
        return None

    expert = None
    try:
        expert = make_expert(role, f"{session_id}:reflect", cwd)
        return await asyncio.wait_for(expert.speak(user, _noop), timeout=timeout)
    except Exception:
        return ""
    finally:
        if expert is not None:
            with contextlib.suppress(Exception):
                await expert.stop()
