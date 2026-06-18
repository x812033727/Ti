"""LLM provider 抽象與工廠。

預設 Claude（走 Agent SDK，自帶工具）。也支援 OpenAI 相容介面（含本地模型，如 Ollama /
LM Studio 透過 OPENAI_BASE_URL）、MiniMax，以及 Codex CLI 非互動模式，讓模型也能「自己 coding」。

所有 backend 都符合 orchestrator 的 ExpertLike 介面：`speak(prompt, broadcast) -> str` 與 `stop()`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
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


def codex_model_for(role: Role) -> str:
    """Codex CLI 模型覆寫；空字串代表沿用 Codex CLI 自身設定。"""
    return config.CODEX_MODEL_LEAD if role.key in config.LEAD_ROLES else config.CODEX_MODEL_FAST


_CODEX_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _clip(text: str, limit: int) -> str:
    """限制內嵌到 prompt / UI 的字串長度，避免一次 Codex 發言把下一輪 prompt 撐爆。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _codex_sandbox_for(role: Role) -> str:
    """依角色工具白名單挑 Codex sandbox；無寫檔工具的角色用 read-only。"""
    if config.CODEX_SANDBOX != "auto":
        return config.CODEX_SANDBOX
    allowed = set(effective_tools(role))
    return "workspace-write" if allowed & _CODEX_WRITE_TOOLS else "read-only"


def _codex_argv(role: Role, cwd: Path) -> list[str]:
    """建立 codex exec argv；測試可直接檢查，不經 shell。"""
    argv = [
        config.CODEX_BIN,
        "exec",
        "--json",
        "--ephemeral",
        "--cd",
        str(cwd),
    ]
    if config.CODEX_BYPASS_SANDBOX:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += ["--sandbox", _codex_sandbox_for(role), "-c", 'approval_policy="never"']
    argv += ["--color", "never"]
    model = codex_model_for(role)
    if model:
        argv += ["--model", model]
    argv.append("-")
    return argv


def _codex_env() -> dict[str, str]:
    """Codex 子程序環境；CODEX_HOME 留空時沿用父程序預設。"""
    env = os.environ.copy()
    if config.CODEX_HOME:
        env["CODEX_HOME"] = config.CODEX_HOME
    return env


def _usage_get(obj, key: str, default=0):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _usage_int(obj, *keys: str) -> int:
    for key in keys:
        try:
            value = int(_usage_get(obj, key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def _codex_item_tool_summary(item: dict) -> tuple[str, str] | None:
    """把 Codex JSONL item 映射為 Studio 的工具事件摘要。"""
    kind = str(item.get("type") or "")
    if kind == "command_execution":
        command = str(item.get("command") or "").strip()
        return ("Bash", "執行: " + _clip(command.splitlines()[0] if command else "", 120))
    if kind in ("file_change", "file_operation"):
        path = str(item.get("path") or item.get("file") or "").strip()
        action = str(item.get("action") or kind)
        return ("Edit", _clip(f"{action}: {path}" if path else action, 120))
    if kind in ("mcp_tool_call", "tool_call"):
        name = str(item.get("name") or item.get("tool") or "tool")
        return (name, _clip(name, 120))
    if kind == "web_search":
        query = str(item.get("query") or "").strip()
        return ("WebSearch", _clip(query, 120))
    return None


class CodexExpert:
    """以 `codex exec` 非互動模式驅動的專家。

    Codex CLI 自己負責工具執行與檔案修改；本類只負責把角色 prompt 餵進 CLI、把 JSONL 事件轉成
    Ti Studio 事件，並維持短期文字歷史，讓同一位專家的下一次 speak() 有基本脈絡。
    """

    def __init__(self, role: Role, session_id: str, cwd: Path):
        self.role = role
        self.session_id = session_id
        self.cwd = cwd
        self._history: list[tuple[str, str]] = []
        self._proc = None
        self._stop_lock = asyncio.Lock()

    async def speak(self, prompt: str, broadcast) -> str:
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        try:
            text = await self._run_codex(prompt, broadcast)
            if text:
                self._history.append((_clip(prompt, 1200), _clip(text, 2400)))
                self._history = self._history[-4:]
            return text
        finally:
            await broadcast(events.expert_status(self.session_id, r.key, "idle"))

    async def stop(self) -> None:
        async with self._stop_lock:
            proc = self._proc
            if proc is None:
                return
            if proc.returncode is not None:
                if self._proc is proc:
                    self._proc = None
                return
            self._terminate(proc)
            reaped = await self._wait_for_proc(proc)
            if reaped and self._proc is proc:
                self._proc = None

    def _prompt(self, prompt: str) -> str:
        parts = [
            self.role.system_prompt,
            "",
            "你現在由 Codex CLI 非互動模式執行。",
            f"工作目錄：{self.cwd}",
            f"本角色允許工具語意：{', '.join(effective_tools(self.role)) or '無'}",
            "請只在工作目錄內行動；需要修改檔案或執行命令時，使用 Codex 可用工具完成。",
            "執行搜尋或讀檔命令時請收斂輸出，例如使用精準路徑、`rg -n <pattern> <path>`、"
            "`sed -n` 或 `head`；避免 `rg .`、大量 `cat`、全 repo 無限制輸出。",
        ]
        if self._history:
            parts.append("\n最近對話脈絡（僅供延續，不代表新指令）：")
            for i, (old_prompt, old_text) in enumerate(self._history, start=1):
                parts.append(f"[{i}] 使用者/流程要求：\n{old_prompt}\n[{i}] 你的回覆：\n{old_text}")
        parts.append("\n本輪要求：")
        parts.append(prompt)
        return "\n".join(parts)

    async def _run_codex(self, prompt: str, broadcast) -> str:
        argv = _codex_argv(self.role, self.cwd)
        final_messages: list[str] = []
        errors: list[str] = []
        stderr_tail = ""
        proc = None
        stdout_task = None
        stderr_task = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.cwd),
                env=_codex_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._proc = proc
        except FileNotFoundError:
            return await self._system_note(
                "【系統】找不到 Codex CLI，請確認 TI_CODEX_BIN 或 PATH。", broadcast
            )

        try:
            assert proc.stdin is not None
            proc.stdin.write(self._prompt(prompt).encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            with contextlib.suppress(Exception):
                await proc.stdin.wait_closed()

            loop = asyncio.get_running_loop()
            started_at = loop.time()
            last_activity = started_at

            async def _stdout() -> None:
                nonlocal last_activity
                assert proc is not None and proc.stdout is not None
                async for raw in proc.stdout:
                    last_activity = loop.time()
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        errors.append(_clip(line, 500))
                        continue
                    text = await self._handle_codex_event(data, broadcast)
                    if text:
                        final_messages.append(text)
                    if data.get("type") in ("error", "turn.failed"):
                        errors.append(_clip(json.dumps(data, ensure_ascii=False), 1000))

            async def _stderr() -> str:
                assert proc is not None and proc.stderr is not None
                buf = bytearray()
                while True:
                    chunk = await proc.stderr.read(1024)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > 8000:
                        del buf[:-8000]
                return buf.decode("utf-8", "replace")

            stdout_task = asyncio.create_task(_stdout())
            stderr_task = asyncio.create_task(_stderr())
            timeout_reason = ""
            try:
                while True:
                    if proc.returncode is not None:
                        break
                    now = loop.time()
                    deadlines: list[tuple[str, float]] = []
                    if config.TURN_HARD_TIMEOUT:
                        deadlines.append(
                            ("總時長", started_at + float(config.TURN_HARD_TIMEOUT) - now)
                        )
                    if config.TURN_IDLE_TIMEOUT:
                        deadlines.append(
                            ("閒置", last_activity + float(config.TURN_IDLE_TIMEOUT) - now)
                        )
                    if not deadlines:
                        await proc.wait()
                        break
                    deadline_reason, wait_s = min(deadlines, key=lambda item: item[1])
                    if wait_s <= 0:
                        timeout_reason = deadline_reason
                        raise TimeoutError
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=wait_s)
                        break
                    except TimeoutError:
                        now = loop.time()
                        hard_expired = bool(
                            config.TURN_HARD_TIMEOUT
                            and now - started_at >= float(config.TURN_HARD_TIMEOUT)
                        )
                        idle_expired = bool(
                            config.TURN_IDLE_TIMEOUT
                            and now - last_activity >= float(config.TURN_IDLE_TIMEOUT)
                        )
                        if hard_expired:
                            timeout_reason = "總時長"
                            raise
                        if idle_expired:
                            timeout_reason = "閒置"
                            raise
            except TimeoutError:
                self._terminate(proc)
                await proc.wait()
                with contextlib.suppress(Exception):
                    await stdout_task
                stderr_tail = await stderr_task
                limit = (
                    config.TURN_IDLE_TIMEOUT
                    if timeout_reason == "閒置"
                    else config.TURN_HARD_TIMEOUT
                )
                return await self._system_note(
                    f"【系統】Codex CLI 發言逾時中止（{timeout_reason or '總時長'}上限 {float(limit):g} 秒）。",
                    broadcast,
                )
            stdout_error: Exception | None = None
            try:
                await stdout_task
            except Exception as exc:  # noqa: BLE001 — Codex JSONL/事件轉送失敗要降級成模型訊息
                stdout_error = exc
                errors.append(f"{type(exc).__name__}: {exc}")
            stderr_tail = await stderr_task

            text = "\n".join(m for m in final_messages if m).strip()
            if text:
                return text
            if stdout_error is not None:
                return await self._system_note(
                    "【系統】Codex CLI 事件解析失敗，已中止本輪發言避免整場討論崩潰。\n"
                    + _clip(f"{type(stdout_error).__name__}: {stdout_error}", 1200),
                    broadcast,
                )
            if proc.returncode:
                detail = _clip(stderr_tail or "\n".join(errors), 2000)
                note = f"【系統】Codex CLI 執行失敗（exit {proc.returncode}）。"
                if detail:
                    note += f"\n{detail}"
                return await self._system_note(note, broadcast)
            if errors:
                return await self._system_note(
                    "【系統】Codex CLI 未產生可用回覆。\n" + "\n".join(errors[-3:]),
                    broadcast,
                )
            return ""
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                self._terminate(proc)
                await self._wait_for_proc(proc)
            for task in (stdout_task, stderr_task):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            raise
        finally:
            if self._proc is proc:
                self._proc = None

    async def _handle_codex_event(self, data: dict, broadcast) -> str:
        typ = data.get("type")
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if typ == "item.started":
            summary = _codex_item_tool_summary(item)
            if summary is not None:
                await broadcast(events.expert_status(self.session_id, self.role.key, "working"))
                await broadcast(events.tool_use(self.session_id, self.role.key, *summary))
            return ""
        if typ == "item.completed" and item.get("type") == "agent_message":
            text = str(item.get("text") or "").strip()
            if text:
                await broadcast(
                    events.expert_message(
                        self.session_id,
                        self.role.key,
                        self.role.name,
                        self.role.avatar,
                        text,
                    )
                )
            return text
        return ""

    async def _system_note(self, note: str, broadcast) -> str:
        await broadcast(
            events.expert_message(
                self.session_id, self.role.key, self.role.name, self.role.avatar, note
            )
        )
        return note

    async def _wait_for_proc(self, proc, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return True
        except ProcessLookupError:
            return True
        except asyncio.TimeoutError:
            return False

    def _terminate(self, proc) -> None:
        pid = getattr(proc, "pid", None)
        if pid is not None and hasattr(os, "killpg") and hasattr(os, "getpgid"):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


class OpenAIExpert:
    """以 OpenAI 相容 chat completions + function-calling 工具迴圈驅動的專家。

    chat 是注入的 async callable(messages, tools, model) -> response，方便測試替換。
    """

    def __init__(
        self,
        role: Role,
        session_id: str,
        cwd: Path,
        chat,
        model: str,
        provider: str = "openai",
    ):
        self.role = role
        self.session_id = session_id
        self.cwd = cwd
        self._chat = chat
        self._model = model
        self._provider = provider
        self._tools = tools.specs_for(effective_tools(role))
        self._messages: list[dict] = [{"role": "system", "content": role.system_prompt}]
        # per-speak 去重快取：speak() 入口會換上新實例（防跨 speak 結果洩漏）。此處先建一份，
        # 避免有人新增旁路呼叫路徑時踩 AttributeError（架構決策）。
        self._dedup_cache = tools.DedupCache()

    async def speak(self, prompt: str, broadcast) -> str:
        """送出 prompt，跑 function-calling 工具迴圈，回傳完整發言文字。

        整個工具迴圈打包為單一 `_attempt`，交核心 `llm_caller.run_with_retries` 控制退避，
        與 Claude 端（experts._speak_with_retries）共用同一 `make_retry_config()` 旋鈕：
        - 命中 429／5xx（限流／過載）時走有限次退避重試；非限流 API 錯誤與重試耗盡皆回退空字串
          （對齊既有 except→"" 行為），未知例外由骨幹原樣 re-raise，不掩蓋真錯。
        - idle 廣播置於 `finally`，覆蓋成功／限流耗盡／api_error／未知例外四路徑。

        429 多發生在 `_chat`（LLM 呼叫）階段，工具執行本身不觸發限流。整輪工具迴圈被重放時，
        寫入型／非冪等工具的重執行防護由 **providers 層 per-speak `_dedup_cache`** 處理
        （`tools.execute_deduped`：同一 key 第二次命中回首次結果、不重跑副作用），非 tools.execute
        層。Claude provider 路徑尚無此保護（已開核心 backlog 票，見任務 #2 架構決策）。
        """
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        cfg = make_retry_config()
        # speak 進入時快照訊息歷史；user 訊息與 collected 都搬進 _attempt，retry 時以
        # snapshot + [user_msg] 還原，確保多次嘗試不把歷史重複累加。
        snapshot = list(self._messages)
        user_msg = {"role": "user", "content": prompt}
        # 每次 speak 重建去重快取：scope=單次 speak，跨 speak 不共用（防前一輪結果洩漏）。
        self._dedup_cache = tools.DedupCache()

        async def _attempt() -> str:
            self._messages[:] = snapshot + [user_msg]
            # 每個 attempt 重置 attempt-內出現序號（保留跨 attempt 的結果快取），確保
            # retry 重放整輪迴圈時同位置的非冪等呼叫對齊回首次 key、命中不重執行副作用。
            self._dedup_cache.new_attempt()
            collected: list[str] = []
            usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
            for _ in range(config.OPENAI_MAX_STEPS):
                resp = await self._chat(self._messages, self._tools, self._model)
                resp_usage = getattr(resp, "usage", None)
                if resp_usage is not None:
                    prompt_tokens = _usage_int(resp_usage, "prompt_tokens", "input_tokens")
                    completion_tokens = _usage_int(resp_usage, "completion_tokens", "output_tokens")
                    total_tokens = _usage_int(resp_usage, "total_tokens") or (
                        prompt_tokens + completion_tokens
                    )
                    usage["prompt"] += prompt_tokens
                    usage["completion"] += completion_tokens
                    usage["total"] += total_tokens
                    usage["calls"] += 1
                msg = resp.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None) or []
                self._messages.append(_assistant_dict(msg, tool_calls))

                if tool_calls:
                    await broadcast(events.expert_status(self.session_id, r.key, "working"))
                    for tc in tool_calls:
                        name = tc.function.name
                        args = tools.parse_args(tc.function.arguments)
                        result = await tools.execute_deduped(
                            name, args, self.cwd, self._dedup_cache
                        )
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

            if usage["calls"]:
                await broadcast(
                    events.token_usage(
                        self.session_id,
                        r.key,
                        self._provider,
                        self._model,
                        usage["prompt"],
                        usage["completion"],
                        usage["total"],
                    )
                )
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
        max_retries=0,  # 讓位給 run_with_retries，避免 SDK 內建重試與外層退避雙層疊乘
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
    if prov == "codex":
        return CodexExpert(role, session_id, cwd)
    if prov in ("openai", "minimax"):
        return OpenAIExpert(
            role,
            session_id,
            cwd,
            chat=_chat_for(prov),
            model=openai_model_for(role),
            provider=prov,
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
