"""Expert — 包裝一個 Claude Agent SDK 客戶端，代表一位具名專家。

每位專家是獨立的 ClaudeSDKClient，維持自己的對話脈絡（記得先前討論）。speak() 會把
SDK 串流回來的訊息轉成 StudioEvent，透過注入的 broadcast callback 即時送出，並回傳這次
發言的完整文字供 Orchestrator 解析決議。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import claude_accounts, claude_usage, config, conventions, events, lint, llm_caller, tools
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

# interrupt()/disconnect() 在疑似 wedged 的 stdio 控制通道上的硬上限（秒）。這兩個呼叫
# 都走可能已死鎖的控制通道；未加逾時時，一旦通道卡住，發言層 watchdog 的回收（_abort_turn）
# 與 session.run 收尾（orchestrator finally → Expert.stop）都會永久卡死，連外層任務逾時
# 取消都無法收斂（見 issue #286）。統一以此上限圈住，逾時即改走 best-effort SIGKILL＋重建。
_CTRL_TIMEOUT = 30.0


class ExpertTurnTimeout(Exception):
    """專家發言逾時（idle＝串流久無進展；hard＝總時長超過上限）。

    max_turns 限不住單一工具呼叫卡死（如前景跑常駐 server），由此例外把卡住的
    turn 標記出來，partial_text 保留逾時前已收到的文字供失敗回饋使用。
    """

    def __init__(self, reason: str, partial_text: str):
        super().__init__(reason)
        self.reason = reason  # "idle" | "hard"
        self.partial_text = partial_text


class ExpertRateLimited(llm_caller.RateLimitSignal):
    """experts 層的限流訊號別名——交由 speak 層做有限次 retry-after 退避重試。

    分類／退避邏輯已上收到核心 `llm_caller`；本子類僅保留 experts 慣用名，並讓
    `llm_caller.classify_failure` 以 isinstance(RateLimitSignal) 統一辨識。
    """


class ExpertOverloaded(llm_caller.OverloadedSignal):
    """experts 層的過載（529）訊號別名——交由 speak 層做有限次「純指數退避」重試。

    與 429（retry-after 退避）分屬不同退避策略；重試耗盡後與其它 API 錯誤共用 fallback。
    """


class ExpertAPIError(llm_caller.APIErrorSignal):
    """experts 層的 API 錯誤訊號別名——非限流／非過載錯誤文字，視為該輪失敗走 fallback。

    與限流／過載分屬獨立失敗路徑：不重試，直接回傳不含核可關鍵詞的系統說明文字。
    """


# 失敗 fallback 文字的穩定標記子字串——單一事實來源，供下游（如冒煙報告）以純消費端
# 方式從 transcript 計數 429／SDK 錯誤文字命中，避免兩端字串各寫一份而漂移。
RATE_LIMIT_FALLBACK_MARKER = "因 API 限流（429）"
API_ERROR_FALLBACK_MARKER = "發言收到 API 錯誤"

# 錯誤文字分類器已上收到核心 `llm_caller`（provider 無關，可被 experts／providers 共用）。
# 此處保留 experts 慣用的私有名作為穩定別名，呼叫端與既有測試無需改動。
_classify_api_text = llm_caller.classify_api_text
_classify_failure = llm_caller.classify_failure


def _backoff_delay(retry_after: float | None, attempt: int) -> float:
    """退避秒數：委派核心 `llm_caller.backoff_delay`，base／cap 由 experts 的 config 帶入。

    本薄包裝在呼叫時讀取 config（而非載入期），故設定頁／測試 monkeypatch config 後即時生效。
    """
    return llm_caller.backoff_delay(
        retry_after,
        attempt,
        base=config.EXPERT_RATE_LIMIT_BACKOFF,
        cap=config.EXPERT_RATE_LIMIT_BACKOFF_CAP,
        jitter=config.EXPERT_RATE_LIMIT_BACKOFF_JITTER,
    )


async def _sleep(seconds: float) -> None:
    """退避等待的注入縫：測試 monkeypatch 本函式即可零實際等待並記錄延遲。

    實作委派核心 `llm_caller._default_sleep`，不再重複維護 sleep body（消除重複邏輯）；
    此薄包裝僅作為 experts 層的 monkeypatch 接點，傳入 run_with_retries 的 sleep。
    """
    await llm_caller._default_sleep(seconds)


def make_retry_config() -> llm_caller.RetryConfig:
    """工廠：call-time 讀 config 退避四值，回傳統一的 `RetryConfig` 物件。

    這是 experts 層退避策略的**單一真實來源**——`_speak_with_retries` 只需取得本物件、
    再經 `cfg.as_kwargs()` 平鋪傳入 `run_with_retries`，取代散傳 max_retries/backoff/sleep。

    config 取用時機：
    - `max_retries` 於本工廠呼叫時讀 `config.EXPERT_RATE_LIMIT_RETRIES`（並 clamp ≥0，
      讓外部合約清晰、防呆在最近端），故設定頁／測試 monkeypatch config 後即時反映。
    - `base/cap/jitter` 於本工廠呼叫時自 `config.EXPERT_RATE_LIMIT_BACKOFF`／`_CAP`／`_JITTER`
      帶入 `RetryConfig` 對應欄位——讓回傳物件的欄位值**與實際退避行為一致**（避免「物件欄位
      顯示預設 0，實際退避卻用 config 0.5」的不一致；高工審查指出），並符合 task #3 spec 的
      `RetryConfig(max_retries=, cap=, jitter=)` 統一入口寫法。
    - `backoff` 仍顯式注入模組級 lazy 函式 `_backoff_delay`（`__post_init__` 對顯式 backoff 不
      覆蓋），使其於**被呼叫時（retry 當下）**才讀 config——保留 lazy-read 語意（架構決策＋QA
      反向假綠對照 `test_negative_control_distinguishes_lazy_from_snapshot` 鎖死，禁建構快照）。
      欄位（建構快照）與 backoff（retry lazy）同源同一組 config 鍵，常態下一致。
    - `sleep` 引用模組級 `_sleep`（測試 monkeypatch 接點）。
    """
    return llm_caller.RetryConfig(
        max_retries=max(0, config.EXPERT_RATE_LIMIT_RETRIES),
        base=config.EXPERT_RATE_LIMIT_BACKOFF,
        cap=config.EXPERT_RATE_LIMIT_BACKOFF_CAP,
        jitter=config.EXPERT_RATE_LIMIT_BACKOFF_JITTER,
        backoff=_backoff_delay,
        sleep=_sleep,
    )


def _make_retry_observer(role_key: str) -> llm_caller.Observer:
    """experts 層的結構化 observe sink：把中介層 task #4 的可觀測事件落成 log。

    本 sink 為**純記錄**接點——只寫 log、不改任何控制流（向後相容鐵則：加裝觀測性不得
    改變既有行為，sink 拋例外亦由 `llm_caller._emit` 吞掉）。它與 idle/hard timeout 正交：
    逾時走 EV_TIMEOUT 獨立事件、不混入退避計數。before_sleep 的人類可讀 log＋broadcast 仍由
    `_on_retry` 負責；本 sink 補上「結構化欄位（kind/delay/total_delay/outcome…）」供 metrics
    收斂。事件名沿用中介層的穩定 `EV_*` 契約，不在 experts 自定義字串。
    """

    def observe(event: str, fields) -> None:
        logger.info("llm_retry 專家=%s 事件=%s %s", role_key, event, dict(fields))

    return observe


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


def _make_fs_guard_hook(cwd: Path):
    """產生綁定該專家 cwd 的 PreToolUse hook：硬擋寫檔工具寫到 cwd 之外。

    並行 lane 隔離的真正防線。Claude provider 的專家用 SDK 內建 Write/Edit/MultiEdit/
    NotebookEdit，**不經** studio 的 `tools.execute`／`safe_resolve`；其檔案寫入是否限制在
    cwd 內全靠 claude-agent-sdk 的 CLI 沙箱 FS 邊界——而 `SandboxSettings` 無 FS 欄位，該
    邊界在巢狀沙箱／缺依賴時會靜默失效（實測：sandbox 開著、寫 cwd 外兄弟目錄仍成功），
    導致 lane 專家把成果寫到主工作樹，使並行隔離名不副實、合併變 no-op／撞未追蹤檔。

    為何用 PreToolUse hook 而非 `can_use_tool`：`can_use_tool` 只對「未預先允許」的工具諮詢，
    寫檔工具在 allowed_tools 內已預先允許，且工程師等角色用 `permission_mode="acceptEdits"`
    會自動接受編輯而完全跳過 `can_use_tool`（實測 hook 0 次呼叫）。PreToolUse hook 則對所有
    工具呼叫一律先行、不受 allow-list／permission_mode 影響，是唯一可靠的攔截點。

    回傳 deny（permissionDecision=deny）擋下 cwd 外的寫入；其餘一律放行（回 {}）。只擋寫、
    不擋讀（避免誤傷研究讀取）。序列模式 cwd＝主工作目錄，專家本就在其中寫檔，不受影響。
    """
    root = Path(cwd)

    async def pre_tool_use(input_data, tool_use_id, context):
        tool_name = (input_data or {}).get("tool_name", "")
        if tool_name in _WRITE_TOOLS:
            data = (input_data or {}).get("tool_input", {}) or {}
            raw = str(data.get("file_path") or data.get("notebook_path") or data.get("path") or "")
            if raw and not _path_within(root, raw):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"工作隔離：禁止寫入工作目錄（{root}）之外的路徑（{raw}）"
                        ),
                    }
                }
        return {}

    return pre_tool_use


# 寫時 lint 只掛在「會改 .py 內容」的三個工具上（NotebookEdit 是 ipynb、刻意不含）。
_LINT_TOOLS = ("Write", "Edit", "MultiEdit")


def _make_lint_hook(cwd: Path):
    """產生綁定該專家 cwd 的 PostToolUse hook：寫入/編輯 .py 後就地 ruff 修復＋回饋殘餘違規。

    治「lint 事後才紅」（#249/#496/#364/#367 連續三輪各燒 1-2 小時只為空格）：問題在寫檔
    的當下被修掉/回饋，不再穿越整場 session 到收尾閘門才爆。回饋走 additionalContext
    （溫和注入，不用 block——寫檔已成功，標成失敗只會誤導）；lint.lint_file 內部 fail-open
    （非 .py/無 ruff/逾時/例外一律 None），hook 這層再兜一層，絕不擋工具流程。
    """
    root = Path(cwd)

    async def post_tool_use(input_data, tool_use_id, context):
        try:
            tool_name = (input_data or {}).get("tool_name", "")
            if tool_name not in _LINT_TOOLS:  # matcher 已過濾，這裡是防禦性雙保險
                return {}
            data = (input_data or {}).get("tool_input", {}) or {}
            raw = str(data.get("file_path") or "")
            if not raw:
                return {}
            feedback = await lint.lint_file(root, raw)
            if feedback:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": feedback,
                    }
                }
        except Exception:  # noqa: BLE001 — 寫時 lint 絕不能弄死工具呼叫
            logger.debug("lint hook 失敗（靜默放行）", exc_info=True)
        return {}

    return post_tool_use


def _scoped_exhausted(model: str, models_usage: dict, threshold: float) -> bool:
    """該 claude 模型是否撞上「按模型 scoped」週限。

    models_usage＝claude_usage.fetch_rate_limits()["models"]，鍵為模型 display_name（如
    "Fable"）。比對規則（display_name 小寫出現在 model id 小寫內，涵蓋 fable→claude-fable-5）
    集中在 ``claude_accounts.scoped_used_pct``（SSOT，帳號輪替第 1.5 層亦用同一比對）；
    用量達 threshold 即視為撞滿。
    """
    pct = claude_accounts.scoped_used_pct(model, models_usage)
    return pct is not None and pct >= threshold


def _reroute_if_scoped_exhausted(model: str) -> str:
    """在線 claude 帳號對 model 的 scoped 週限已達門檻時，改派非 scoped 備援模型（預設 Opus）。

    補額度閘門盲點：provider_quota 只看全域 5h/7d、刻意不含 scoped 週限，故 Fable 週限滿了
    閘門仍判 claude「可用」→ 釘 Fable 的專家一直撞滿額度空轉。此處在每次建 session（LLM 呼叫前）
    無條件攔一手，把撞滿 scoped 的模型換成走全域額度的備援，讓工作室續跑到週限重置。
    保守原則：額度查不到（error）、無 scoped 資訊、未撞門檻、或備援自身也撞 scoped → 一律不改派。
    """
    fallback = config.CLAUDE_SCOPED_FALLBACK_MODEL
    threshold = config.CLAUDE_SCOPED_LIMIT_THRESHOLD
    if not fallback or not model or model == fallback:
        return model
    rl = claude_usage.fetch_rate_limits()
    if rl.get("error"):
        return model
    models_usage = rl.get("models") or {}
    if not models_usage or not _scoped_exhausted(model, models_usage, threshold):
        return model
    if _scoped_exhausted(fallback, models_usage, threshold):
        # 備援也撞 scoped 週限 → 改派無益，維持原模型，交回既有額度閘門/帳號輪替處理。
        return model
    logger.warning(
        "claude 模型 %s scoped 週限達 %.0f%%，本場自動改派備援模型 %s（走全域 weekly 額度）",
        model,
        threshold,
        fallback,
    )
    return fallback


def _model_for(role: Role) -> str:
    """在建立專家時（每個 session）即時讀取設定，讓模型選擇變更可於下次討論生效。

    優先序：PM 釘選（config.PM_PIN_MODEL——PM 是分派/檢驗/表決的決策者，判斷品質須穩定，
    預設釘 claude-fable-5；設空字串＝解除釘選）→ 該角色的個別覆寫（config.ROLE_MODELS，
    設定面板「<角色>模型」欄位）→ 沒覆寫（auto）就沿用 LEAD_ROLES → MODEL_LEAD/FAST 的二分法。
    末段套 _reroute_if_scoped_exhausted：選定的模型若在線帳號 scoped 週限已滿，改派備援模型。
    """
    if role.key == "pm" and config.PM_PIN_MODEL:
        model = config.PM_PIN_MODEL
    else:
        override = config.ROLE_MODELS.get(role.key, "")
        if override:
            model = override
        else:
            model = config.MODEL_LEAD if role.key in config.LEAD_ROLES else config.MODEL_FAST
    return _reroute_if_scoped_exhausted(model)


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


def _usage_get(obj, key: str, default=0):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _usage_int(obj, key: str) -> int:
    try:
        return int(_usage_get(obj, key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _stream_block_has_content(block) -> bool:
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return bool(text.strip())
    return hasattr(block, "name") and hasattr(block, "input")


async def _emit_claude_token_usage(
    msg,
    session_id: str,
    role: Role,
    broadcast: Broadcast,
    *,
    ttft_s: float | None = None,
    model: str | None = None,
) -> None:
    usage = getattr(msg, "usage", None)
    if usage is None:
        return
    prompt = _usage_int(usage, "input_tokens")
    completion = _usage_int(usage, "output_tokens")
    total = prompt + completion
    if total <= 0:
        return
    # Claude 端時延直取 SDK ResultMessage.duration_api_ms（API 通訊時間），不自造計時；無則 None。
    duration_ms = getattr(msg, "duration_api_ms", None)
    await broadcast(
        events.token_usage(
            session_id,
            role.key,
            "claude",
            model or _model_for(role),
            prompt,
            completion,
            total,
            cost_usd=getattr(msg, "total_cost_usd", None),
            duration_ms=duration_ms,
            ttft_s=ttft_s,
            cache_read=_usage_int(usage, "cache_read_input_tokens"),
            cache_write=_usage_int(usage, "cache_creation_input_tokens"),
        )
    )


def _expert_hooks(cwd: Path) -> dict:
    """組專家的 hooks 設定：PreToolUse FS guard 恆在；寫時 lint（PostToolUse）受旋鈕保護。"""
    from claude_agent_sdk import HookMatcher

    hooks: dict = {"PreToolUse": [HookMatcher(matcher=None, hooks=[_make_fs_guard_hook(cwd)])]}
    if config.EXPERT_LINT_HOOK:
        hooks["PostToolUse"] = [
            HookMatcher(matcher="Write|Edit|MultiEdit", hooks=[_make_lint_hook(cwd)], timeout=60)
        ]
    return hooks


def _build_client(role: Role, session_id: str, cwd: Path, model: str = ""):
    """建立該專家的 ClaudeSDKClient。

    抽成模組級函式以開出注入縫：測試可 monkeypatch 本函式回傳假 client，
    從而在未安裝 claude-agent-sdk、不連線的情況下驗證 Expert 生命週期。
    執行期內容與原 __init__ 完全相同。``model`` 非空時覆寫 _model_for(role)
    （per-task 派工／招募指定模型）；空＝缺省行為不變。

    # 重試由 speak() 層的 run_with_retries 統一管控；ClaudeSDKClient 本身不做額外退避，避免雙層疊乘。
    # ClaudeAgentOptions 不暴露 max_retries 旋鈕（與 OpenAI SDK 不同），故無需也無從顯式設 0。
    # 【可控層邊界】Python SDK 層（query.py／subprocess_cli.py）原始碼確認無 429/529 retry 邏輯；
    # CLI subprocess（Node.js 層）是否對 API 429/529 做內部 retry 不可從 Python SDK 原始碼驗證，
    # 為已知邊界——types.py:api_error_status 顯示 CLI 最終確實透傳 429/529，但透傳前的嘗試次數未知。
    # 切勿在此 client 層另加任何重試/退避旋鈕，否則會與外層 run_with_retries 疊乘。
    # 對比 OpenAI 路徑：openai SDK 內建 max_retries（預設 2），須在
    # providers.py 的 AsyncOpenAI(...) 另行顯式設 max_retries=0 才能達到同等的「單層退避」語意
    # （該對應義務由 OpenAI 路徑各自落實）。
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    return ClaudeSDKClient(
        options=ClaudeAgentOptions(
            system_prompt=role.system_prompt,
            allowed_tools=effective_tools(role),
            permission_mode=role.permission_mode,
            can_use_tool=_auto_allow_tool,
            # PreToolUse hook 把寫檔限制在該專家的 cwd 內（並行 lane 隔離的真正防線；
            # can_use_tool 對預先允許的寫檔工具不觸發，見 _make_fs_guard_hook 說明）。
            # PostToolUse hook（受旋鈕保護）＝寫時 lint：.py 寫入後就地 ruff 修復＋回饋。
            hooks=_expert_hooks(cwd),
            sandbox=config.expert_sandbox_settings(),
            cwd=str(cwd),
            model=model or _model_for(role),
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
    model: str | None = None,
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
    request_sent_at = loop.time()
    ttft_s: float | None = None
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
        msg_arrived_at = loop.time()
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
                            if hit[0] == "overloaded":
                                raise ExpertOverloaded(str(hit[1]), text[:300], partial)
                            raise ExpertAPIError(str(hit[1]), text[:300], partial)
                        unavailable = llm_caller.provider_unavailable_kind(text)
                        if unavailable is not None and unavailable[0] in {
                            "usage_limit",
                            "quota",
                            "billing",
                        }:
                            raise ExpertAPIError(unavailable[0], text[:300], "\n".join(collected))
                        collected.append(text)
                        if ttft_s is None and _stream_block_has_content(block):
                            ttft_s = msg_arrived_at - request_sent_at
                        await broadcast(
                            events.expert_message(
                                session_id, role.key, role.name, role.avatar, text
                            )
                        )
                elif isinstance(block, ToolUseBlock):
                    if ttft_s is None and _stream_block_has_content(block):
                        ttft_s = msg_arrived_at - request_sent_at
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
            await _emit_claude_token_usage(
                msg, session_id, role, broadcast, ttft_s=ttft_s, model=model
            )
            break
    return "\n".join(collected)


class Expert:
    def __init__(self, role: Role, session_id: str, cwd: Path, model: str = ""):
        # 慣例卡：執行環境慣例附進 system prompt（依 cwd 分層、無工具角色跳過、冪等）。
        # 在 __init__ 注入而非 make_expert——涵蓋 autopilot 直接建構的路徑（調查分流/自評/拆分）。
        role = conventions.apply(role, cwd)
        self.role = role
        self.session_id = session_id
        self._cwd = cwd  # 逾時斷線後重建 client 需要
        self._model = model  # 非空＝per-task 派工／招募指定的模型覆寫；空＝沿用 _model_for
        self._client = self._new_client()
        self._connected = False

    def effective_model(self) -> str:
        """實際生效的模型（覆寫優先，否則角色模型槽）——供任務結果（task_result）顯示。"""
        return self._model or _model_for(self.role)

    def _new_client(self):
        """建 client（含逾時斷線後重建）。

        無模型覆寫時維持既有三參數呼叫形——大量既有測試以 ``lambda role, sid, cwd: ...``
        monkeypatch `_build_client`，此處不無故破壞其簽名相容。
        """
        if self._model:
            return _build_client(self.role, self.session_id, self._cwd, self._model)
        return _build_client(self.role, self.session_id, self._cwd)

    async def start(self) -> None:
        if not self._connected:
            await self._client.connect()
            self._connected = True

    async def stop(self) -> None:
        if self._connected:
            try:
                # disconnect() 走 stdio 控制通道；通道 wedged 時未加逾時會永久卡死，
                # 且此呼叫落在 session.run 收尾（orchestrator finally）與外層任務逾時
                # 取消的清理路徑上——卡住即拖垮整個 wait_for 取消（issue #286 根因）。
                await asyncio.wait_for(self._client.disconnect(), _CTRL_TIMEOUT)
            except Exception:  # noqa: BLE001 — 含 TimeoutError；斷線卡死不得拖垮回收
                self._best_effort_kill_subprocess()
            finally:
                self._connected = False

    def _best_effort_kill_subprocess(self) -> None:
        """disconnect() 逾時後的兜底：盡力對 SDK 內部子程序送 SIGKILL（整個 process group）。

        純 best-effort——任何 claude_agent_sdk 版本差異（`_transport`／`_process` 形狀改變、
        取不到 pid）都被 except 吞掉，退化成「殘留一個 idle 子程序」而非崩潰。迴圈活性由
        呼叫端『丟棄 client 參考並重建』保證（子程序卡在 ep_poll 零 CPU，殘留成本有限），
        此處只是回收資源，絕不 load-bearing。
        """
        try:
            from . import runner

            transport = getattr(self._client, "_transport", None)
            proc = getattr(transport, "_process", None)
            if proc is not None and getattr(proc, "pid", None) is not None:
                runner.kill_process_group(proc)
        except Exception:  # noqa: BLE001 — 兜底殺程序失敗不得影響回收流程
            logger.debug("best-effort 殺 SDK 子程序失敗（忽略）", exc_info=True)

    async def speak(self, prompt: str, broadcast: Broadcast) -> str:
        """送出 prompt，串流回應為事件，回傳完整文字。

        受 config.TURN_IDLE_TIMEOUT / TURN_HARD_TIMEOUT 的發言層 watchdog 保護：
        逾時不拋例外，改回傳「【系統】逾時中止」說明文字——其中不含任何核可關鍵詞，
        QA／審查的解析自然視為未通過，走既有的失敗回饋／停滯收斂路徑，orchestrator
        無需任何改動。timeout 放在這裡而非 _speak 包裝層，使 _debate、架構決策等
        直呼 speak 的路徑同樣受保護。

        注意：connect／query 的前置（見 `_attempt`）也受同一組逾時保護並走 `_abort_turn`，
        故 `start()` 改由 `_attempt` 內部呼叫，不在此處先連線。
        """
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        try:
            return await self._speak_with_retries(prompt, broadcast)
        finally:
            await broadcast(events.expert_status(self.session_id, r.key, "idle"))

    async def _speak_with_retries(self, prompt: str, broadcast: Broadcast) -> str:
        """送 prompt 並串流；統一走核心 `llm_caller.run_with_retries` 重試骨幹。

        本方法只負責「把 experts 的零件接上中介層」，不再自維護退避迴圈：
        - attempt_fn＝一次 `query()`＋`stream_to_events()`（架構決策：例外型 429 可能在
          query 階段拋出，只包串流會漏接，故整段包進 attempt_fn 由骨幹重試）。
        - `ExpertTurnTimeout` 透過 passthrough 走 `_abort_turn`——逾時是另一條獨立失敗
          路徑，骨幹不把它當限流退避吞掉。
        - before_sleep 的 log＋broadcast("thinking") 掛在 on_retry hook。
        - 限流重試耗盡／非限流 API 錯誤皆收斂到 `_fallback_note`（不含核可關鍵詞）。
        - 未知例外由骨幹原樣 re-raise，不掩蓋真錯。
        """
        r = self.role
        cfg = make_retry_config()

        async def _attempt() -> str:
            # start()（首次 connect）與 query()（把 prompt 送進 bundled Claude CLI 子程序）都在
            # stream_to_events 之前，不受其 idle／hard watchdog 保護。子程序若卡在連線／送出，
            # 整輪會靜默（無 tool／訊息／token）直到外層 AUTOPILOT_TASK_TIMEOUT（3600s）才被砍
            # ——實測 security 專家第三輪 query() 卡 ~38 分。用同一組 turn 預算把前置也圈進來，
            # 逾時當「無進展（idle）」中止，交由 passthrough→_abort_turn interrupt／重建 client
            # 殺掉卡住的子程序（與串流逾時同一條收斂路徑）。start() 為 idempotent，重試時若已
            # 連線即 no-op；_abort_turn 重建 client 後下次 attempt 會自動重連。
            setup_budget = config.TURN_IDLE_TIMEOUT or config.TURN_HARD_TIMEOUT or None
            try:
                if setup_budget is not None:
                    await asyncio.wait_for(self.start(), setup_budget)
                    await asyncio.wait_for(self._client.query(prompt), setup_budget)
                else:
                    await self.start()
                    await self._client.query(prompt)
            except TimeoutError:
                raise ExpertTurnTimeout("idle", "") from None
            return await stream_to_events(
                self._client.receive_response(),
                self.session_id,
                r,
                broadcast,
                idle_timeout=config.TURN_IDLE_TIMEOUT or None,
                hard_timeout=config.TURN_HARD_TIMEOUT or None,
                model=self.effective_model(),
            )

        async def _on_retry(attempt: int, limit: int, delay: float, snippet: str) -> None:
            logger.warning(
                "專家 %s 撞限流／過載（429／529，第 %d/%d 次重試），退避 %.1fs：%s",
                r.key,
                attempt + 1,
                limit,
                delay,
                snippet,
            )
            await broadcast(events.expert_status(self.session_id, r.key, "thinking"))

        async def _on_rate_limit_exhausted(snippet: str, partial: str) -> str:
            logger.warning(
                "專家 %s 限流重試耗盡（%d 次），暫停 claude provider：%s",
                r.key,
                cfg.max_retries,
                snippet,
            )
            detail = (snippet or partial).strip() or (
                f"claude rate limit exhausted after {cfg.max_retries} retries"
            )
            raise llm_caller.ProviderUnavailable("claude", detail[:2000])

        async def _on_api_error(snippet: str, partial: str) -> str:
            logger.warning("專家 %s 收到 API 錯誤文字，走 fallback：%s", r.key, snippet)
            unavailable = llm_caller.provider_unavailable_kind(snippet)
            if unavailable is not None and unavailable[0] in {
                "usage_limit",
                "quota",
                "billing",
                "rate_limit",
            }:
                detail = (snippet or partial or unavailable[1]).strip()
                raise llm_caller.ProviderUnavailable("claude", detail[:2000])
            return await self._fallback_note(
                f"【系統】{API_ERROR_FALLBACK_MARKER}，本輪中止。", partial, broadcast
            )

        async def _on_timeout(exc: BaseException) -> str:
            return await self._abort_turn(exc, broadcast)

        # 接上中介層 task #4 的可觀測接點：metrics 累加退避次數/延遲，observe sink 落結構化 log。
        # 兩者皆純記錄、不改控制流；逾時走 passthrough 獨立路徑（outcome="timeout"），與退避正交。
        metrics = llm_caller.RetryMetrics()
        text = await llm_caller.run_with_retries(
            _attempt,
            **cfg.as_kwargs(),
            on_retry=_on_retry,
            on_rate_limit_exhausted=_on_rate_limit_exhausted,
            on_api_error=_on_api_error,
            passthrough=(ExpertTurnTimeout,),
            on_passthrough=_on_timeout,
            metrics=metrics,
            observe=_make_retry_observer(r.key),
        )
        if metrics.retries or metrics.outcome not in ("success", ""):
            logger.info("專家 %s 發言收斂：%s", r.key, metrics.to_dict())
        return text

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

            # interrupt()／drain 都走 stdio 控制通道；通道 wedged 時未加逾時會永久卡死，
            # 使本回收路徑（發言層 watchdog 觸發後的收斂）自己也死鎖。以 _CTRL_TIMEOUT 圈住
            # interrupt()；其 TimeoutError 為 Exception 子類，會自然落到下方斷線/重建分支。
            await asyncio.wait_for(self._client.interrupt(), _CTRL_TIMEOUT)

            async def _drain() -> None:
                async for msg in self._client.receive_response():
                    if isinstance(msg, ResultMessage):
                        return

            await asyncio.wait_for(_drain(), _CTRL_TIMEOUT)
        except Exception:
            try:
                await asyncio.wait_for(self._client.disconnect(), _CTRL_TIMEOUT)
            except Exception:  # noqa: BLE001 — 含 TimeoutError；斷線也卡就 SIGKILL 兜底
                self._best_effort_kill_subprocess()
            self._connected = False
            self._client = self._new_client()  # 重建沿用同一模型覆寫（若有）
            note += "（會話無法中斷，已重建；此前脈絡遺失）"
        if exc.partial_text:
            note += f"\n逾時前的部分輸出：\n{exc.partial_text}"
        await broadcast(events.expert_message(self.session_id, r.key, r.name, r.avatar, note))
        return note
