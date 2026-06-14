"""任務 #4：wiring 測試——證明 OpenAIExpert.speak 與 complete_once 實收的退避參數
確實源自 `make_retry_config()`（讀 config 當前值），含改 config 反向對照排假綠。

核心斷言策略（不靠 pytest-mock，純 monkeypatch 自製 spy）：
- spy `llm_caller.run_with_retries`：攔截實際傳入的 **kwargs，斷言 max_retries／backoff／
  sleep 三參數源自 experts.make_retry_config 工廠（backoff/sleep 為 experts 模組級函式本體）。
- spy `providers.make_retry_config`：斷言每次 speak 恰好呼叫工廠一次（無散傳第二套退避）。
- 反向對照：monkeypatch `config.EXPERT_RATE_LIMIT_RETRIES` 後 spy 斷言 max_retries 隨之改變，
  證明是 call-time 讀 config，不是 import 期快照（否則改 config 無效＝假綠）。
- classify_failure 對偽造 OpenAI RateLimitError 的行為顯式斷言（禁止「假設能命中」）。
- 限流耗盡時 speak 回 "" 且不含核可關鍵詞，且 chat 實被委派 1+max_retries 次（反向排假綠）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from studio import config, experts, llm_caller, providers
from studio.roles import BY_KEY


# --- 共用 fakes ----------------------------------------------------------------


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


class FakeChat:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    async def __call__(self, messages, tools, model):
        self.calls += 1
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


class ExplodingChat:
    """每次呼叫都拋同一個（偽 OpenAI）限流例外，用於耗盡路徑測試。"""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def __call__(self, messages, tools, model):
        self.calls += 1
        raise self.exc


async def _noop_broadcast(ev):
    return None


def _spy_run_with_retries(monkeypatch):
    """攔截 llm_caller.run_with_retries 的 kwargs，並照常委派真實實作。

    providers/experts 皆以 `llm_caller.run_with_retries(...)` 取用（call-time 屬性查找），
    故 monkeypatch 模組屬性即可同時覆蓋兩端呼叫點。
    """
    captured: dict = {"count": 0, "kwargs_list": []}
    real = llm_caller.run_with_retries

    async def spy(attempt_fn, **kwargs):
        captured["count"] += 1
        captured["kwargs_list"].append(kwargs)
        captured["last"] = kwargs
        return await real(attempt_fn, **kwargs)

    monkeypatch.setattr(llm_caller, "run_with_retries", spy)
    return captured


def _spy_make_retry_config(monkeypatch):
    """攔截 providers 端綁定的 make_retry_config，計次並照常回傳真實 config。"""
    box = {"count": 0}
    real = providers.make_retry_config

    def spy():
        box["count"] += 1
        return real()

    monkeypatch.setattr(providers, "make_retry_config", spy)
    return box


def _setup_openai(monkeypatch, *, ready=True, offline=False):
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OFFLINE_MODE", offline)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://local" if ready else "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")


# --- ① speak 實收退避三參數源自 make_retry_config -------------------------------


@pytest.mark.asyncio
async def test_speak_wires_retry_config_three_params(monkeypatch, tmp_path):
    """speak 把 make_retry_config() 的 max_retries/backoff/sleep 三參數平鋪傳入
    run_with_retries，且工廠恰好被呼叫一次（無第二套散傳退避）。"""
    cap = _spy_run_with_retries(monkeypatch)
    factory = _spy_make_retry_config(monkeypatch)
    chat = FakeChat([_msg(content="完成發言")])
    expert = providers.OpenAIExpert(BY_KEY["engineer"], "t", tmp_path, chat=chat, model="m")

    out = await expert.speak("實作", _noop_broadcast)

    assert out == "完成發言"
    assert factory["count"] == 1  # speak 只呼叫工廠一次
    assert cap["count"] == 1  # 只進一次退避骨幹，無雙層
    kw = cap["last"]
    # 三參數源自工廠：max_retries 為 config 當前值；backoff/sleep 為 experts 模組級函式本體
    assert kw["max_retries"] == max(0, config.EXPERT_RATE_LIMIT_RETRIES)
    assert kw["backoff"] is experts._backoff_delay
    assert kw["sleep"] is experts._sleep


# --- ② 反向對照：改 config，max_retries 隨之變（證 call-time 讀值，非 import 快照）-----


@pytest.mark.asyncio
async def test_speak_max_retries_tracks_config_reverse_control(monkeypatch, tmp_path):
    cap = _spy_run_with_retries(monkeypatch)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 7)
    chat = FakeChat([_msg(content="ok")])
    expert = providers.OpenAIExpert(BY_KEY["engineer"], "t", tmp_path, chat=chat, model="m")

    await expert.speak("x", _noop_broadcast)

    assert cap["last"]["max_retries"] == 7  # 隨 config 改變 → 非 import 期快照（排假綠）
    assert 7 != 3  # 與預設值 3 不同，反向對照成立


@pytest.mark.asyncio
async def test_speak_max_retries_clamped_non_negative(monkeypatch, tmp_path):
    """工廠對負值 clamp ≥0，避免退避次數為負的未定義行為。"""
    cap = _spy_run_with_retries(monkeypatch)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", -5)
    chat = FakeChat([_msg(content="ok")])
    expert = providers.OpenAIExpert(BY_KEY["pm"], "t", tmp_path, chat=chat, model="m")

    await expert.speak("x", _noop_broadcast)

    assert cap["last"]["max_retries"] == 0


# --- ③ complete_once 路徑同樣收斂於同一退避入口 ---------------------------------


@pytest.mark.asyncio
async def test_complete_once_wires_retry_config_from_config(monkeypatch, tmp_path):
    """complete_once → make_expert → OpenAIExpert.speak 一線到底，退避參數仍源自 config；
    且 complete_once 本層**不**自套第二層 run_with_retries（架構決策：無雙層重試）。"""
    _setup_openai(monkeypatch)
    cap = _spy_run_with_retries(monkeypatch)
    factory = _spy_make_retry_config(monkeypatch)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 5)
    fake = FakeChat([_msg(content="反思結論")])
    monkeypatch.setattr(providers, "_openai_chat", fake)

    out = await providers.complete_once("sys", "user", session_id="s", cwd=tmp_path, timeout=2.0)

    assert out == "反思結論"
    assert factory["count"] == 1  # 僅 speak 一層呼叫工廠
    assert cap["count"] == 1  # 僅一層退避骨幹（complete_once 不套第二層）
    assert cap["last"]["max_retries"] == 5  # 反向對照：源自 config 當前值


# --- ④ classify_failure 對偽造 OpenAI RateLimitError 的顯式行為斷言 --------------


class FakeOpenAIRateLimitError(Exception):
    """模擬 openai.RateLimitError：str(exc) 帶 OpenAI 風格的 429 封包文字。"""


def test_classify_failure_openai_rate_limit_no_retry_after():
    """OpenAI 限流字串（type=rate_limit_exceeded，無 retry-after）→ rate_limit；
    retry_after 為 None＝走固定退避路徑（可接受，明文標注，非靜默退化）。"""
    exc = FakeOpenAIRateLimitError(
        "Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-4o', "
        "'type': 'rate_limit_exceeded', 'code': 'rate_limit_exceeded'}}"
    )
    kind, retry_after, snippet, _partial = llm_caller.classify_failure(exc)
    assert kind == "rate_limit"
    assert retry_after is None  # 無 retry-after → 走 backoff 固定退避（可接受）
    assert "429" in snippet


def test_classify_failure_openai_rate_limit_with_retry_after():
    """帶 Retry-After 時必須解析成非 None（證明能命中 retry-after 優先路徑）。"""
    exc = FakeOpenAIRateLimitError("Error code: 429 - rate limited. Retry-After: 30")
    kind, retry_after, _snippet, _partial = llm_caller.classify_failure(exc)
    assert kind == "rate_limit"
    assert retry_after == 30.0


def test_classify_failure_openai_non_ratelimit_paths():
    """反向對照：4xx 非限流 → api_error（不重試）；無關例外 → unknown（re-raise 不掩蓋）。"""
    bad_req = FakeOpenAIRateLimitError("Error code: 400 - invalid request")
    assert llm_caller.classify_failure(bad_req)[0] == "api_error"
    assert llm_caller.classify_failure(ValueError("unrelated boom"))[0] == "unknown"


# --- ④ 限流耗盡：speak 回 "" 且不含核可關鍵詞，chat 實被委派 1+max_retries 次 -------


@pytest.mark.asyncio
async def test_speak_rate_limit_exhausted_returns_empty_no_keyword(monkeypatch, tmp_path):
    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(experts, "_sleep", _noop_sleep)  # 零實際等待
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 2)
    chat = ExplodingChat(FakeOpenAIRateLimitError("Error code: 429 - rate_limit_exceeded"))
    expert = providers.OpenAIExpert(BY_KEY["senior"], "t", tmp_path, chat=chat, model="m")

    out = await expert.speak("審查", _noop_broadcast)

    assert out == ""  # 耗盡回退空字串
    for kw in ("核可", "完成", "通過", "決議"):
        assert kw not in out  # 不含任何核可關鍵詞，下游解析自然視為未過
    # 反向排假綠：確實重試到耗盡＝1 次首發 + max_retries 次重試（非 guard 短路）
    assert chat.calls == 1 + 2
