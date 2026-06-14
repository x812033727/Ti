"""429／SDK 錯誤文字防線單元測試（experts.py）。

沿用 test_experts.py / test_experts_timeout.py 的注入縫：sys.modules 注入假
claude_agent_sdk、monkeypatch experts._build_client、monkeypatch experts._sleep
以零實際等待並記錄退避延遲。全程不需真 SDK、不連線、不打 api.anthropic.com。

涵蓋（驗收 #4）：
- 偵測器 _classify_api_text 的錨定判別（含反向黑樣本：正常引用 error/429 字樣不誤殺）。
- stream_to_events 命中 rate_limit／api_error 文字時拋對應例外、不進 transcript。
- speak 層：例外型 429 觸發退避重試後成功；重試耗盡走 fallback；錯誤文字走 fallback；
  未知例外不被吞（re-raise）；逾時與限流為獨立路徑。
"""

from __future__ import annotations

import sys
import types

import pytest

from studio import config, events, experts
from studio.roles import BY_KEY

# --- 共用 ---------------------------------------------------------------


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


async def _agen(items):
    for it in items:
        yield it


@pytest.fixture
def fake_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        pass

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ToolUseBlock:
        def __init__(self, name, input):
            self.name = name
            self.input = input

    mod.AssistantMessage = AssistantMessage
    mod.ResultMessage = ResultMessage
    mod.TextBlock = TextBlock
    mod.ToolUseBlock = ToolUseBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


_RATE_LIMIT_JSON = '{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
_OVERLOADED_JSON = '{"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}'


# --- _classify_api_text：錨定判別 + 反向黑樣本 --------------------------


def test_classify_rate_limit_error_json():
    assert experts._classify_api_text(_RATE_LIMIT_JSON) == ("rate_limit", None)


def test_classify_rate_limit_reads_retry_after():
    text = _RATE_LIMIT_JSON + " retry-after: 7"
    assert experts._classify_api_text(text) == ("rate_limit", 7.0)


def test_classify_status_429_is_rate_limit():
    assert experts._classify_api_text("API Error: status code 429 too many requests") == (
        "rate_limit",
        None,
    )


def test_classify_overloaded_is_api_error():
    assert experts._classify_api_text(_OVERLOADED_JSON) == ("api_error", "overloaded_error")


def test_classify_http_503_is_api_error():
    assert experts._classify_api_text("API Error: HTTP 503 service unavailable") == (
        "api_error",
        "HTTP 503",
    )


@pytest.mark.parametrize(
    "text",
    [
        # 反向黑樣本：正常發言引用錯誤字樣，無 JSON 錯誤封包、無 status 前綴 → 不誤殺
        "回應 @架構師: 同意。我們之前撞到 rate limit error 與 429 錯誤，但已經修好了。",
        "建議對 overloaded error 做退避重試，避免整場崩潰。",
        "這支測試有 429 個案例，error 訊息要更清楚。",
        "",
        "回應 @工程師: 反對，理由是邊界沒處理好。",
    ],
)
def test_classify_normal_speech_not_misclassified(text):
    assert experts._classify_api_text(text) is None


# --- stream_to_events：命中即拋、不進 transcript ------------------------


async def test_stream_raises_rate_limited_with_partial(fake_sdk):
    role = BY_KEY["engineer"]
    bucket, broadcast = collect()
    msgs = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("先講正常的一句")]),
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_RATE_LIMIT_JSON)]),
    ]
    with pytest.raises(experts.ExpertRateLimited) as ei:
        await experts.stream_to_events(_agen(msgs), "s", role, broadcast)
    assert ei.value.partial_text == "先講正常的一句"
    # 錯誤文字未被廣播為正常訊息（只有前一句合法文字進了 transcript）
    texts = [ev.payload.get("text", "") for ev in bucket]
    assert "先講正常的一句" in texts
    assert all("rate_limit_error" not in t for t in texts)


async def test_stream_raises_api_error_on_overloaded(fake_sdk):
    role = BY_KEY["engineer"]
    _, broadcast = collect()
    msgs = [fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_OVERLOADED_JSON)])]
    with pytest.raises(experts.ExpertAPIError) as ei:
        await experts.stream_to_events(_agen(msgs), "s", role, broadcast)
    assert ei.value.kind == "overloaded_error"


async def test_stream_normal_speech_with_error_words_passes(fake_sdk):
    """反向黑樣本（串流層）：發言含 error/429 字樣仍正常完成、不被殺。"""
    role = BY_KEY["engineer"]
    _, broadcast = collect()
    line = "回應 @架構師: 同意，雖然之前撞到 rate limit error 與 429，但已修好"
    msgs = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(line)]),
        fake_sdk.ResultMessage(),
    ]
    text = await experts.stream_to_events(_agen(msgs), "s", role, broadcast)
    assert text == line


# --- speak 層：退避重試 / fallback / re-raise ---------------------------


@pytest.fixture
def _rl_config(monkeypatch):
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 2)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)


@pytest.fixture
def _record_sleep(monkeypatch):
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(experts, "_sleep", fake_sleep)
    return delays


class _ScriptedClient:
    """query 依序套用 query_effects（exc 或 None）；receive_response 吐 stream_msgs。

    query_effects 每項：Exception 實例＝該次 query 拋出（模擬例外型 429）；None＝正常。
    """

    def __init__(self, fake_sdk, query_effects, stream_msgs):
        self._sdk = fake_sdk
        self._effects = list(query_effects)
        self._stream = stream_msgs
        self.queries = 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt):
        eff = self._effects[self.queries] if self.queries < len(self._effects) else None
        self.queries += 1
        if isinstance(eff, Exception):
            raise eff

    def receive_response(self):
        async def gen():
            for m in self._stream:
                yield m

        return gen()


def _make_expert(monkeypatch, client):
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: client)
    return experts.Expert(BY_KEY["engineer"], "sess", "/tmp/x")


async def test_speak_exception_429_retries_then_succeeds(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """例外型 429（query 階段拋出）退避後重試成功——驗證退避包住 query()。"""
    exc = RuntimeError(_RATE_LIMIT_JSON + " retry-after: 1")
    ok_stream = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("重試後完成發言")]),
        fake_sdk.ResultMessage(),
    ]
    client = _ScriptedClient(fake_sdk, query_effects=[exc], stream_msgs=ok_stream)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert text == "重試後完成發言"
    assert client.queries == 2  # 初次 + 1 次重試
    assert _record_sleep == [1.0]  # 讀到 retry-after=1


async def test_speak_rate_limit_text_retries_then_fallback(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """限流文字每次都命中，重試耗盡 → 走 fallback（不含核可詞、照常進 transcript）。"""
    rl_stream = [fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_RATE_LIMIT_JSON)])]
    client = _ScriptedClient(fake_sdk, query_effects=[], stream_msgs=rl_stream)
    exp = _make_expert(monkeypatch, client)
    bucket, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert "限流" in text and "中止" in text
    assert "核可" not in text and "同意" not in text  # 下游不會誤判為通過
    assert client.queries == 3  # 初次 + 2 次重試（RETRIES=2）
    assert len(_record_sleep) == 2  # 指數退避：2.0, 4.0
    assert _record_sleep == [2.0, 4.0]
    # fallback 說明有進 transcript（廣播 expert_message）
    assert any("中止" in ev.payload.get("text", "") for ev in bucket)


async def test_speak_api_error_text_fallback_no_retry(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """非限流 API 錯誤文字（overloaded）→ 直接 fallback、不重試。"""
    stream = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("中止前的半句")]),
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_OVERLOADED_JSON)]),
    ]
    client = _ScriptedClient(fake_sdk, query_effects=[], stream_msgs=stream)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert "API 錯誤" in text and "中止" in text
    assert "中止前的半句" in text  # partial 文字被帶入 fallback
    assert client.queries == 1  # 不重試
    assert _record_sleep == []


async def test_speak_unknown_exception_reraised(fake_sdk, monkeypatch, _rl_config, _record_sleep):
    """未知例外不被吞，原樣 re-raise，不掩蓋真正的程式錯誤。"""
    client = _ScriptedClient(fake_sdk, query_effects=[ValueError("boom")], stream_msgs=[])
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    with pytest.raises(ValueError, match="boom"):
        await exp.speak("做點事", broadcast)
    assert client.queries == 1
    assert _record_sleep == []


def test_backoff_delay_prefers_retry_after_and_caps(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 10.0)
    assert experts._backoff_delay(5.0, 0) == 5.0  # 採 retry-after
    assert experts._backoff_delay(99.0, 0) == 10.0  # retry-after 也夾 cap
    assert experts._backoff_delay(None, 0) == 2.0  # 指數：2 × 2^0
    assert experts._backoff_delay(None, 2) == 8.0  # 2 × 2^2
    assert experts._backoff_delay(None, 5) == 10.0  # 夾 cap


def test_backoff_delay_jitter_off_equals_pure_exponential(monkeypatch):
    """旗標顯式關閉時，指數分支回傳純指數值（與舊行為等價），且完全不呼叫 jitter。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 10.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", False)
    # 旗標關閉時若誤呼叫 random.uniform，即視為回歸
    monkeypatch.setattr(
        experts.random, "uniform", lambda *a: pytest.fail("旗標關閉時不該呼叫 jitter")
    )
    assert experts._backoff_delay(None, 0) == 2.0  # 2 × 2^0
    assert experts._backoff_delay(None, 2) == 8.0  # 2 × 2^2
    assert experts._backoff_delay(None, 5) == 10.0  # 夾 cap
    assert experts._backoff_delay(None, 30) == 10.0  # 大 attempt 仍夾 cap


def test_backoff_delay_jitter_within_ceiling(monkeypatch):
    """旗標開啟時，指數分支回傳值落在 [0, min(base*2^n, cap)]，永不為負/超過 cap。"""
    import random as _random

    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 10.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", True)
    _random.seed(1234)  # 固定種子讓結果可重現
    for attempt, ceiling in [(0, 2.0), (1, 4.0), (2, 8.0), (3, 10.0), (5, 10.0), (30, 10.0)]:
        for _ in range(500):
            v = experts._backoff_delay(None, attempt)
            assert 0.0 <= v <= ceiling, (attempt, v, ceiling)


def test_backoff_delay_jitter_uses_full_jitter_args(monkeypatch):
    """旗標開啟時呼叫 random.uniform(0, ceiling)，ceiling 為 min(base*2^n, cap)。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 10.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", True)
    calls = []
    monkeypatch.setattr(experts.random, "uniform", lambda lo, hi: calls.append((lo, hi)) or hi)
    assert experts._backoff_delay(None, 2) == 8.0  # ceiling = 2 × 2^2
    assert experts._backoff_delay(None, 5) == 10.0  # ceiling 夾 cap
    assert calls == [(0, 8.0), (0, 10.0)]


def test_backoff_delay_jitter_on_retry_after_not_jittered(monkeypatch):
    """旗標開啟時，retry_after 分支仍回傳 min(retry_after, cap)，完全不抖。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 10.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", True)
    # 若誤抖動，uniform 被呼叫即拋錯
    monkeypatch.setattr(
        experts.random, "uniform", lambda *a: pytest.fail("retry_after 分支不該呼叫 jitter")
    )
    assert experts._backoff_delay(5.0, 0) == 5.0
    assert experts._backoff_delay(99.0, 0) == 10.0  # 夾 cap，不抖
