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


def effective_provider(role: Role) -> str:
    """角色的有效 provider：per-role 覆寫（TI_PROVIDER_<KEY>）優先，否則全域 PROVIDER。

    讓 Claude／MiniMax 可混用——例如把 tool-calling 吃重的工程師／QA 留 Claude、
    討論型角色走 MiniMax。
    """
    return config.role_provider(role.key) or config.PROVIDER


def openai_model_for(role: Role) -> str:
    """OpenAI 相容路徑（openai／minimax）的角色模型：依 LEAD_ROLES 二分。

    依角色的「有效 provider」決定模型槽：minimax 走 MiniMax 自家模型，其餘走 OpenAI 模型槽
    ——故未顯式覆寫時行為與既有 openai 完全一致。
    """
    lead = role.key in config.LEAD_ROLES
    if effective_provider(role) == "minimax":
        return config.MINIMAX_MODEL_LEAD if lead else config.MINIMAX_MODEL_FAST
    return config.OPENAI_MODEL_LEAD if lead else config.OPENAI_MODEL_FAST


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


def _openai_client_args(provider: str | None = None) -> tuple[str, str | None]:
    """依 provider 選 chat-completions 客戶端的 (api_key, base_url)。

    provider 省略時取全域 config.PROVIDER。minimax 與 openai 共用同一相容客戶端，僅憑證/
    端點來源不同；抽成純函式以便在未安裝 openai 套件的環境下也能單元測試憑證分流
    （不污染對方的金鑰）。
    """
    if (provider or config.PROVIDER) == "minimax":
        return (config.MINIMAX_API_KEY or "sk-none", config.MINIMAX_BASE_URL or None)
    return (config.OPENAI_API_KEY or "sk-none", config.OPENAI_BASE_URL or None)


async def _openai_chat(messages, tools_, model, provider=None):
    import openai

    api_key, base_url = _openai_client_args(provider)
    client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
    )
    return await client.chat.completions.create(
        model=model, messages=messages, tools=tools_ or None
    )


def _chat_for(provider: str):
    """產生綁定特定 provider 憑證的 chat 閉包（供混用時每位專家各用自己的 provider）。

    仍經模組級 `_openai_chat`（測試以 monkeypatch 替換的接縫），只是固定帶入該 provider。
    """

    async def chat(messages, tools_, model):
        return await _openai_chat(messages, tools_, model, provider=provider)

    return chat


def make_expert(role: Role, session_id: str, cwd: Path):
    """依角色的「有效 provider」建立一位專家（支援 Claude／MiniMax 混用）。"""
    prov = effective_provider(role)
    if prov in ("openai", "minimax"):
        return OpenAIExpert(
            role, session_id, cwd, chat=_chat_for(prov), model=openai_model_for(role)
        )
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

    限流（429／5xx）退避職責分層：本層**刻意不**自套第二層 `run_with_retries`（架構決策
    否決雙層重試——避免與 speak() 內部退避疊加成語義不清的指數退避）。退避是 `speak()`
    層的職責，**兩端皆已收斂**於同一 `make_retry_config()` 的 `EXPERT_RATE_LIMIT_*` 旋鈕：
    - Claude 端 `experts.Expert.speak()` 經 `_speak_with_retries()` → `run_with_retries`
      做有限次退避，耗盡才回退空字串；此端限流不會冒泡到本層。
    - OpenAI 端 `OpenAIExpert.speak()` 同樣經 `run_with_retries` 吸收限流（429／5xx），
      耗盡才回退空字串；此端限流亦不會冒泡到本層。
    因此下方 `except Exception` 是最終兜底（非「限流永不走到這」）：吞掉逾時
    （`asyncio.wait_for` 的 `asyncio.TimeoutError`）與未預期錯誤（含上游骨幹原樣 re-raise
    的未知例外），維持「永不 raise」合約；並記 warning（含 traceback）供生產診斷，不靜默吞噬。
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
        # 最終兜底層：守住「永不 raise」合約。退避是 speak() 層職責，兩端皆已收斂於
        # run_with_retries（Claude 經 _speak_with_retries、OpenAI 經 OpenAIExpert.speak），
        # 限流在 speak 層被吸收、不冒泡到此；本層刻意不套第二層重試（架構決策否決雙層退避）。
        # 這裡吞逾時（wait_for 的 asyncio.TimeoutError）與未預期錯誤；記 warning（含 traceback）
        # 避免靜默吞噬難以診斷。
        logger.warning("complete_once 降級回空字串（session=%s）", session_id, exc_info=True)
        return ""
    finally:
        if expert is not None:
            with contextlib.suppress(Exception):
                await expert.stop()
