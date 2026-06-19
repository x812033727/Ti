"""QA 驗證（任務 #2）：OpenAIExpert.speak() 接 run_with_retries 退避行為。

驗收標準對應：
- 命中限流（429）／過載（529）走 run_with_retries 有限次退避重試。
- 非限流 API 錯誤（4xx/5xx，如 401/503）立即回退空字串、不重試。
- 限流重試耗盡回退空字串，且不含核可關鍵詞（partial broadcast 不一致風險）。
- 未知例外不被掩蓋（由骨幹 re-raise，不被當成 fallback 吞掉）。
- 退避三參數源自 make_retry_config()（與 Claude 端共用 EXPERT_RATE_LIMIT_* 旋鈕）；
  含反向對照：改 config 後 run_with_retries 實收 max_retries 隨之變（非 import 快照）。
- classify_failure 對偽造 OpenAI RateLimitError 顯式斷言（design 標 🔴 高風險，禁「假設能命中」）。
- 重試時訊息歷史不重複累加（snapshot 還原）。
- idle 廣播覆蓋成功／耗盡／api_error 三路徑。
- 全程用 FakeChat 注入，零 SDK 依賴、零實際等待。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from studio import config, events, experts, llm_caller, providers
from studio.roles import BY_KEY

# 核可關鍵詞：耗盡回退時絕不可出現（上層靠關鍵詞判核可，空字串污染會誤判）。
_APPROVAL_KEYWORDS = ("核可", "決議", "通過", "approve", "LGTM")


# --- 測試替身 ----------------------------------------------------------------


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


class ScriptedChat:
    """依 actions 腳本逐次回應：元素為 Exception→raise；否則→當作 response 回傳。

    腳本用盡後重複最後一個動作（方便「永遠失敗」case）。記錄呼叫次數與每次 messages。
    """

    def __init__(self, actions):
        self.actions = actions
        self.calls = 0
        self.seen: list[list] = []

    async def __call__(self, messages, tools, model):
        self.seen.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        action = self.actions[idx]
        if isinstance(action, BaseException):
            raise action
        return action


class HangingChat:
    """永不回應的 chat，用來驗證 OpenAI-compatible watchdog 會中止。"""

    def __init__(self):
        self.calls = 0

    async def __call__(self, messages, tools, model):
        self.calls += 1
        await asyncio.Event().wait()


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


# OpenAI SDK RateLimitError.str() 近似形態：「Error code: 429 - {...}」。
def _rate_limit_err(retry_after: int | None = None):
    msg = "Error code: 429 - {'error': {'message': 'Rate limit reached', 'type': 'tokens'}}"
    if retry_after is not None:
        msg = f"Error code: 429 - slow down. Retry-After: {retry_after}"
    return RuntimeError(msg)


def _overloaded_err():
    return RuntimeError("Error code: 529 - overloaded_error")


def _auth_err():
    return RuntimeError("Error code: 401 - invalid api key")


def _statuses(bucket):
    return [e.payload["status"] for e in bucket if e.type == events.EventType.EXPERT_STATUS]


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """零實際等待：make_retry_config 取 experts._sleep，patch 它即不真睡。

    同時記錄每次退避秒數，供斷言「確實有退避」。"""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(experts, "_sleep", fake_sleep)
    return delays


@pytest.fixture
def _cfg(monkeypatch):
    def _apply(*, retries, base=2.0, cap=60.0, jitter=0.0):
        monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", retries)
        monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", base)
        monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", cap)
        monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", jitter)

    return _apply


def _expert(chat, role=None, tmp_path=None):
    return providers.OpenAIExpert(
        role or BY_KEY["engineer"], "sess", tmp_path or "/tmp", chat=chat, model="m"
    )


# === 0) classify_failure 對偽造 OpenAI 例外（design 🔴 高風險，顯式斷言）=========


def test_classify_fake_openai_rate_limit_no_retry_after():
    """無 Retry-After 的 OpenAI 429：須命中 rate_limit；retry_after=None → 走固定退避（可接受）。"""
    kind, retry_after, snippet, _ = llm_caller.classify_failure(_rate_limit_err())
    assert kind == "rate_limit"
    assert retry_after is None  # 明文標注：無建議值，退避骨幹改走指數退避
    assert "429" in snippet


def test_classify_fake_openai_rate_limit_with_retry_after():
    """帶 Retry-After 的 OpenAI 429：須命中 rate_limit 且能解析出建議秒數（非 None）。"""
    kind, retry_after, _, _ = llm_caller.classify_failure(_rate_limit_err(retry_after=5))
    assert kind == "rate_limit"
    assert retry_after == 5.0  # 禁「假設能命中」——顯式證實 retry-after 優先生效


def test_classify_fake_openai_non_ratelimit_is_api_error():
    """401/503 等非限流錯誤須歸 api_error（立即 fallback、不重試）。"""
    assert llm_caller.classify_failure(_auth_err())[0] == "api_error"
    assert llm_caller.classify_failure(RuntimeError("Error code: 503 - down"))[0] == "api_error"


def test_classify_unknown_not_masked():
    """純未知例外歸 unknown（由骨幹 re-raise，不被當 fallback 吞掉）。"""
    assert llm_caller.classify_failure(RuntimeError("connection reset"))[0] == "unknown"


# === 1) 429 退避後成功 =========================================================


@pytest.mark.asyncio
async def test_speak_retries_on_429_then_succeeds(_cfg, _no_wait, tmp_path):
    """前兩次 429、第三次成功：回傳正確文字，chat 共呼叫 3 次，退避發生 2 次。"""
    _cfg(retries=3)
    chat = ScriptedChat([_rate_limit_err(), _rate_limit_err(), _msg(content="完成發言")])
    bucket, broadcast = collect()

    out = await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert out == "完成發言"
    assert chat.calls == 3  # 1 initial + 2 retry
    assert len(_no_wait) == 2  # 確有兩次退避等待
    assert "idle" in _statuses(bucket)  # 成功路徑也廣播 idle


@pytest.mark.asyncio
async def test_speak_retries_on_529_overloaded_then_succeeds(_cfg, _no_wait, tmp_path):
    """過載 529 同屬可退避路徑：一次 529 後成功。"""
    _cfg(retries=3)
    chat = ScriptedChat([_overloaded_err(), _msg(content="ok")])
    bucket, broadcast = collect()

    out = await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert out == "ok"
    assert chat.calls == 2
    assert len(_no_wait) == 1


# === 2) 限流重試耗盡 → 回退空字串、不含核可關鍵詞 ===============================


@pytest.mark.asyncio
async def test_speak_rate_limit_exhausted_returns_empty(_cfg, _no_wait, tmp_path):
    """永遠 429、retries=2：耗盡後回 ""，chat 呼叫 3 次（1+2），退避 2 次。"""
    _cfg(retries=2)
    chat = ScriptedChat([_rate_limit_err()])  # 永遠 raise
    bucket, broadcast = collect()

    out = await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert out == ""
    assert chat.calls == 3  # 1 initial + 2 retry
    assert len(_no_wait) == 2
    # 不含任何核可關鍵詞（耗盡回退不可污染上層核可判定）
    assert not any(k in out for k in _APPROVAL_KEYWORDS)
    assert "idle" in _statuses(bucket)


@pytest.mark.asyncio
async def test_speak_zero_retries_rate_limit_immediate_empty(_cfg, _no_wait, tmp_path):
    """retries=0：429 不退避，立即回 ""，chat 僅 1 次、零退避。"""
    _cfg(retries=0)
    chat = ScriptedChat([_rate_limit_err()])
    bucket, broadcast = collect()

    out = await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert out == ""
    assert chat.calls == 1
    assert len(_no_wait) == 0


@pytest.mark.asyncio
async def test_minimax_rate_limit_exhausted_raises_provider_unavailable(_cfg, _no_wait, tmp_path):
    """MiniMax 429 耗盡要暫停 provider，不能回空字串讓任務被當一般 QA fail 重跑。"""
    _cfg(retries=1)
    chat = ScriptedChat([_rate_limit_err()])
    bucket, broadcast = collect()
    expert = providers.OpenAIExpert(
        BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m", provider="minimax"
    )

    with pytest.raises(providers.ProviderUnavailable) as seen:
        await expert.speak("做事", broadcast)

    assert seen.value.provider == "minimax"
    assert chat.calls == 2
    assert len(_no_wait) == 1
    assert _statuses(bucket)[-1] == "idle"


@pytest.mark.asyncio
async def test_minimax_chat_timeout_raises_provider_unavailable(
    _cfg, _no_wait, monkeypatch, tmp_path
):
    """MiniMax chat 卡住時由 watchdog 轉成 provider unavailable，而不是永久卡住。"""
    _cfg(retries=0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.01)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)
    chat = HangingChat()
    bucket, broadcast = collect()
    expert = providers.OpenAIExpert(
        BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m", provider="minimax"
    )

    with pytest.raises(providers.ProviderUnavailable) as seen:
        await expert.speak("做事", broadcast)

    assert seen.value.provider == "minimax"
    assert "timeout" in str(seen.value).lower()
    assert chat.calls == 1
    assert _statuses(bucket)[-1] == "idle"


# === 3) 非限流 API 錯誤 → 立即回退、不重試 =====================================


@pytest.mark.asyncio
async def test_speak_non_ratelimit_api_error_returns_empty_no_retry(_cfg, _no_wait, tmp_path):
    """401 認證錯誤：立即回 "" 且絕不重試（chat 僅 1 次、零退避）。"""
    _cfg(retries=5)  # 故意給高 retries，證明 api_error 仍不重試
    chat = ScriptedChat([_auth_err()])
    bucket, broadcast = collect()

    out = await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert out == ""
    assert chat.calls == 1  # 反向對照：未重試
    assert len(_no_wait) == 0
    # 與限流耗盡對稱：非限流回退同樣不得含核可關鍵詞（不污染上層核可判定）
    assert not any(k in out for k in _APPROVAL_KEYWORDS)
    assert "idle" in _statuses(bucket)


# === 4) 未知例外不被掩蓋（re-raise）===========================================


@pytest.mark.asyncio
async def test_speak_unknown_exception_propagates(_cfg, _no_wait, tmp_path):
    """未知例外須原樣 re-raise（不被當 fallback 吞成 ""），且仍廣播 idle。"""
    _cfg(retries=3)
    boom = RuntimeError("connection reset by peer")
    chat = ScriptedChat([boom])
    bucket, broadcast = collect()

    with pytest.raises(RuntimeError, match="connection reset"):
        await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert chat.calls == 1  # 未重試
    assert "idle" in _statuses(bucket)  # finally 仍廣播 idle


# === 5) 退避三參數源自 make_retry_config / config（wiring + 反向對照）==========


def _spy(monkeypatch, module, name):
    real = getattr(module, name)
    calls: list[dict] = []

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append({"args": args, "kwargs": kwargs, "result": result})
        return result

    monkeypatch.setattr(module, name, wrapper)
    return calls


@pytest.mark.asyncio
async def test_speak_invokes_make_retry_config_once(_cfg, _no_wait, monkeypatch, tmp_path):
    """speak 一輪恰取用統一工廠 make_retry_config 一次，回傳真 RetryConfig。"""
    _cfg(retries=2)
    spy = _spy(monkeypatch, providers, "make_retry_config")
    chat = ScriptedChat([_msg(content="ok")])
    _, broadcast = collect()

    await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert len(spy) == 1
    assert isinstance(spy[0]["result"], llm_caller.RetryConfig)


@pytest.mark.asyncio
async def test_run_with_retries_receives_config_backed_params(
    _cfg, _no_wait, monkeypatch, tmp_path
):
    """run_with_retries 實收 max_retries/backoff/sleep 源自工廠（＝config 當前值）。"""
    _cfg(retries=4, base=2.0, cap=60.0, jitter=0.0)
    run_calls = _spy(monkeypatch, llm_caller, "run_with_retries")
    chat = ScriptedChat([_msg(content="ok")])
    _, broadcast = collect()

    await _expert(chat, tmp_path=tmp_path).speak("做事", broadcast)

    assert len(run_calls) == 1
    kw = run_calls[0]["kwargs"]
    assert kw["max_retries"] == 4  # ＝ config.EXPERT_RATE_LIMIT_RETRIES
    assert kw["backoff"] is experts._backoff_delay  # 共用 Claude 端 lazy backoff
    assert kw["sleep"] is experts._sleep
    assert kw["backoff"](None, 2) == 8.0  # base 2 × 2^2：config 值真流入


@pytest.mark.asyncio
async def test_reverse_control_config_change_flows_in(_cfg, _no_wait, monkeypatch, tmp_path):
    """反向對照排假綠：改 EXPERT_RATE_LIMIT_RETRIES → run_with_retries 實收 max_retries 隨之變。"""
    run_calls = _spy(monkeypatch, llm_caller, "run_with_retries")
    _, broadcast = collect()

    _cfg(retries=1, base=2.0)
    await _expert(ScriptedChat([_msg(content="a")]), tmp_path=tmp_path).speak("一", broadcast)
    assert run_calls[0]["kwargs"]["max_retries"] == 1
    assert run_calls[0]["kwargs"]["backoff"](None, 0) == 2.0

    _cfg(retries=7, base=3.0)
    await _expert(ScriptedChat([_msg(content="b")]), tmp_path=tmp_path).speak("二", broadcast)
    assert run_calls[1]["kwargs"]["max_retries"] == 7  # 1 → 7：非 import 快照
    assert run_calls[1]["kwargs"]["backoff"](None, 0) == 3.0  # base 2 → 3


# === 6) 重試時訊息歷史不重複累加（snapshot 還原）==============================


@pytest.mark.asyncio
async def test_message_history_not_duplicated_on_retry(_cfg, _no_wait, tmp_path):
    """兩次 429 後成功：最終 self._messages 內 user prompt 只出現一次（snapshot+[user] 還原）。"""
    _cfg(retries=3)
    expert = _expert(
        ScriptedChat([_rate_limit_err(), _rate_limit_err(), _msg(content="done")]),
        tmp_path=tmp_path,
    )
    _, broadcast = collect()

    await expert.speak("唯一的提問", broadcast)

    user_msgs = [m for m in expert._messages if m.get("role") == "user"]
    assert len(user_msgs) == 1  # 不因 3 次嘗試累加成 3 條
    assert user_msgs[0]["content"] == "唯一的提問"


# === 7) 重試耗盡的整體一致性：回 "" 且廣播序列含 idle 收尾 =====================


@pytest.mark.asyncio
async def test_exhausted_no_partial_approval_leak(_cfg, _no_wait, tmp_path):
    """耗盡路徑不得 broadcast 任何 expert_message（避免 partial 核可外洩到上層）。"""
    _cfg(retries=1)
    chat = ScriptedChat([_rate_limit_err()])
    bucket, broadcast = collect()

    out = await _expert(chat, role=BY_KEY["senior"], tmp_path=tmp_path).speak("審查", broadcast)

    assert out == ""
    msg_events = [e for e in bucket if e.type == events.EventType.EXPERT_MESSAGE]
    assert msg_events == []  # 從未產生發言 → 上層不會誤收 partial 核可
    assert _statuses(bucket)[-1] == "idle"  # 收尾必為 idle


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
