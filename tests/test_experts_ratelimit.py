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

from studio import config, events, experts, llm_caller
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
# 真正不重試的 API 錯誤樣本（非 429／529）——驗證「直接 fallback、不退避」路徑。
_AUTH_ERROR_JSON = '{"type":"error","error":{"type":"authentication_error","message":"bad key"}}'


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


def test_classify_overloaded_is_overloaded_retryable():
    # 529／overloaded 改分到可退避的 overloaded 類（純指數退避重試），不再直接 fallback。
    assert experts._classify_api_text(_OVERLOADED_JSON) == ("overloaded", "overloaded_error")


def test_classify_auth_error_is_api_error():
    assert experts._classify_api_text(_AUTH_ERROR_JSON) == ("api_error", "authentication_error")


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


async def test_stream_raises_overloaded_on_overloaded(fake_sdk):
    # 串流命中 overloaded(529) → 拋可退避的 ExpertOverloaded（交 speak 層純指數退避重試）。
    role = BY_KEY["engineer"]
    _, broadcast = collect()
    msgs = [fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_OVERLOADED_JSON)])]
    with pytest.raises(experts.ExpertOverloaded) as ei:
        await experts.stream_to_events(_agen(msgs), "s", role, broadcast)
    assert ei.value.kind == "overloaded_error"


async def test_stream_raises_api_error_on_auth_error(fake_sdk):
    # 非 429／529 的 API 錯誤（authentication_error）→ 拋不重試的 ExpertAPIError。
    role = BY_KEY["engineer"]
    _, broadcast = collect()
    msgs = [fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_AUTH_ERROR_JSON)])]
    with pytest.raises(experts.ExpertAPIError) as ei:
        await experts.stream_to_events(_agen(msgs), "s", role, broadcast)
    assert ei.value.kind == "authentication_error"


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
    # 退避值斷言需確定性 → 關閉 jitter（jitter 行為另有專測）。
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)


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
    """非限流／非過載 API 錯誤文字（authentication_error）→ 直接 fallback、不重試。"""
    stream = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("中止前的半句")]),
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_AUTH_ERROR_JSON)]),
    ]
    client = _ScriptedClient(fake_sdk, query_effects=[], stream_msgs=stream)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert "API 錯誤" in text and "中止" in text
    assert "中止前的半句" in text  # partial 文字被帶入 fallback
    assert client.queries == 1  # 不重試
    assert _record_sleep == []


async def test_speak_overloaded_529_retries_pure_exponential_then_fallback(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """端到端：overloaded(529) 真的會退避重試（純指數退避），耗盡後才走 API 錯誤 fallback。

    驗收 #3：529 走純指數退避＋夾 cap＋最大次數——直接反證「529 不重試」的舊行為已修正。
    """
    ov_stream = [fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_OVERLOADED_JSON)])]
    client = _ScriptedClient(fake_sdk, query_effects=[], stream_msgs=ov_stream)
    exp = _make_expert(monkeypatch, client)
    bucket, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert "API 錯誤" in text and "中止" in text  # 耗盡後走通用 API 錯誤 fallback
    assert "核可" not in text and "同意" not in text  # 下游不會誤判為通過
    assert client.queries == 3  # 初次 + 2 次重試（RETRIES=2）——確實有重試
    assert _record_sleep == [2.0, 4.0]  # 純指數退避（忽略任何 retry-after）、夾 cap=60
    assert any("中止" in ev.payload.get("text", "") for ev in bucket)


async def test_speak_overloaded_529_retry_then_succeeds(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """overloaded(529) 退避後該輪恢復正常 → 回傳正常發言、不走 fallback。"""
    exc = RuntimeError(_OVERLOADED_JSON)  # 例外型 529（query 階段拋出）
    ok_stream = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("過載退避後完成發言")]),
        fake_sdk.ResultMessage(),
    ]
    client = _ScriptedClient(fake_sdk, query_effects=[exc], stream_msgs=ok_stream)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert text == "過載退避後完成發言"
    assert client.queries == 2  # 初次 + 1 次重試
    assert _record_sleep == [2.0]  # 純指數退避：2 × 2^0


async def test_speak_unknown_exception_reraised(fake_sdk, monkeypatch, _rl_config, _record_sleep):
    """未知例外不被吞，原樣 re-raise，不掩蓋真正的程式錯誤。"""
    client = _ScriptedClient(fake_sdk, query_effects=[ValueError("boom")], stream_msgs=[])
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    with pytest.raises(ValueError, match="boom"):
        await exp.speak("做點事", broadcast)
    assert client.queries == 1
    assert _record_sleep == []


# --- wiring 測試（B 法）：monkeypatch 工廠回傳改值，驗退避行為隨之改變 ------
#
# 任務 #3：證明 speak 觸發路徑「真的取用 make_retry_config 的回傳值」來決定重試，
# 而非繞過工廠直讀 config。手法＝patch `experts.make_retry_config` 回傳改造後的
# RetryConfig，注入「每次都命中限流」的串流（保證打到 max_retries 上限），再斷言
# 實際 query 次數＝1+max_retries。注意：本檔 _rl_config 設 config RETRIES=2，但下列
# 測試的重試次數一律跟著「工廠回傳值」走（1 或 3），與 config 的 2 脫鉤——這正是
# wiring 證明：取值路徑經過工廠，而非散讀 config。退避秒數另由 _record_sleep 佐證
# base/cap/jitter 同樣源自工廠回傳。


async def _run_until_rate_limit_exhausted(fake_sdk, monkeypatch, cfg):
    """patch 工廠回傳 cfg，注入永遠命中限流的串流，跑完 speak，回傳 client 供斷言。"""
    monkeypatch.setattr(experts, "make_retry_config", lambda: cfg)
    rl_stream = [fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_RATE_LIMIT_JSON)])]
    client = _ScriptedClient(fake_sdk, query_effects=[], stream_msgs=rl_stream)
    exp = _make_expert(monkeypatch, client)
    bucket, broadcast = collect()
    text = await exp.speak("做點事", broadcast)
    return client, text, bucket


async def test_speak_wiring_b_factory_max_retries_1_retries_once(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """B 法核心：工廠回 max_retries=1 → 實際只重試 1 次（初次 + 1 = 2 次 query）。"""
    cfg = experts.RetryConfig(max_retries=1, base=2.0, cap=60.0, jitter=0.0)
    client, text, _ = await _run_until_rate_limit_exhausted(fake_sdk, monkeypatch, cfg)

    assert client.queries == 2  # 初次 + 1 次重試，跟著工廠的 max_retries=1（非 config 的 2）
    assert _record_sleep == [2.0]  # 只退避 1 次；秒數＝cfg.base，證明 base 亦源自工廠
    assert "限流" in text and "中止" in text  # 耗盡後走 fallback
    assert "核可" not in text and "同意" not in text


async def test_speak_wiring_b_factory_max_retries_3_retries_thrice(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """B 法反向對照：同一 config（RETRIES=2）下，工廠改回 max_retries=3 → 重試 3 次。

    與上一條對照：重試次數 2 vs 4 隨「工廠回傳」改變而非隨 config，排除假綠、坐實
    取值路徑經過 make_retry_config。
    """
    cfg = experts.RetryConfig(max_retries=3, base=2.0, cap=60.0, jitter=0.0)
    client, text, _ = await _run_until_rate_limit_exhausted(fake_sdk, monkeypatch, cfg)

    assert client.queries == 4  # 初次 + 3 次重試，跟著工廠的 max_retries=3（非 config 的 2）
    assert _record_sleep == [2.0, 4.0, 8.0]  # 指數退避 3 次、夾 cap=60，秒數源自 cfg.base/cap
    assert "限流" in text and "中止" in text


async def test_speak_wiring_b_factory_base_drives_backoff(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """B 法補強：工廠回傳的 base 直接決定退避秒數——改 base=5.0，首次退避即為 5.0。"""
    cfg = experts.RetryConfig(max_retries=1, base=5.0, cap=60.0, jitter=0.0)
    client, _, _ = await _run_until_rate_limit_exhausted(fake_sdk, monkeypatch, cfg)

    assert client.queries == 2
    assert _record_sleep == [5.0]  # base 由工廠帶入 → 退避秒數隨之改變（非 config 的 2.0）


async def test_speak_wires_middleware_observability(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """task #5 接線：experts 的 speak 路徑確實接上中介層 task #4 的 metrics/observe 接點。

    驗證（a）退避時 observe sink 收到中介層穩定 EV_* 事件、（b）RetryMetrics 累加退避次數/延遲，
    且為純記錄——對外回傳文字與既有 fallback 語義不變（向後相容）。
    """
    seen: list[str] = []
    monkeypatch.setattr(
        experts, "_make_retry_observer", lambda key: lambda ev, fields: seen.append(ev)
    )
    rl_stream = [fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(_RATE_LIMIT_JSON)])]
    client = _ScriptedClient(fake_sdk, query_effects=[], stream_msgs=rl_stream)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    # 對外行為不變：限流耗盡走 fallback、不含核可詞
    assert "限流" in text and "中止" in text
    assert _record_sleep == [2.0, 4.0]
    # 觀測接點被觸發：退避兩次（EV_RETRY）後收斂到限流耗盡（EV_RATE_LIMIT_EXHAUSTED）
    assert seen.count(llm_caller.EV_RETRY) == 2
    assert llm_caller.EV_RATE_LIMIT_EXHAUSTED in seen


async def test_speak_observe_failure_does_not_break_flow(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """observe sink 拋例外不得影響重試控制流（向後相容鐵則：觀測性不改既有行為）。"""

    def boom_observer(key):
        def observe(ev, fields):
            raise RuntimeError("sink 壞了")

        return observe

    monkeypatch.setattr(experts, "_make_retry_observer", boom_observer)
    exc = RuntimeError(_RATE_LIMIT_JSON + " retry-after: 1")
    ok_stream = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("重試後完成發言")]),
        fake_sdk.ResultMessage(),
    ]
    client = _ScriptedClient(fake_sdk, query_effects=[exc], stream_msgs=ok_stream)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert text == "重試後完成發言"  # sink 爆掉，主流程照常重試成功
    assert client.queries == 2
    assert _record_sleep == [1.0]


def test_backoff_delay_prefers_retry_after_and_caps(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 10.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)  # 確定性斷言
    assert experts._backoff_delay(5.0, 0) == 5.0  # 採 retry-after
    assert experts._backoff_delay(99.0, 0) == 10.0  # retry-after 也夾 cap
    assert experts._backoff_delay(None, 0) == 2.0  # 指數：2 × 2^0
    assert experts._backoff_delay(None, 2) == 8.0  # 2 × 2^2
    assert experts._backoff_delay(None, 5) == 10.0  # 夾 cap


def test_backoff_delay_applies_config_jitter(monkeypatch):
    """experts 呼叫端把 config 的 jitter 旗標傳進核心 backoff_delay（#133）。

    用固定 rand=1.0 取退避上下界：
    - 529／指數路徑 jitter「向下」散開 → nominal×(1−j)；
    - 429／retry-after 路徑 jitter「向上」微抖、夾 cap → min(nominal×(1+j), cap)。
    jitter=0 時兩路徑皆回確定 nominal（與舊行為等價）。
    """
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(llm_caller.random, "random", lambda: 1.0)  # 取 jitter 邊界

    # 預設 jitter 開（0.5）：529 路徑向下散開、429 路徑向上微抖。
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.5)
    assert experts._backoff_delay(None, 0) == 2.0 * (1 - 0.5)  # 1.0
    assert experts._backoff_delay(10.0, 0) == min(10.0 * (1 + 0.5), 60.0)  # 15.0

    # jitter=0：回確定 nominal，行為與關閉時等價。
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)
    assert experts._backoff_delay(None, 0) == 2.0
    assert experts._backoff_delay(10.0, 0) == 10.0


# --- 任務 #4：A 法 wiring 測試（spy 驗「取用路徑」呼叫工廠且回傳值流入骨幹）------
#
# 與 B 法（monkeypatch 工廠回傳改值、驗退避行為隨之改變）互補：A 法「不改變真實行為」，
# 只在原函式外包一層記錄器（語義同 pytest-mock 的 mocker.spy——保留真實實作、額外記錄
# 呼叫），斷言觸發 speak 時：
#   1) make_retry_config 在觸發路徑上被呼叫且「僅一次」；
#   2) 該次呼叫的「回傳值」確實流入 llm_caller.run_with_retries（max_retries 同源）。
#
# 註：本專案 dev 依賴未含 pytest-mock，故以 monkeypatch 自製等效 spy——零外部依賴、
#     可在乾淨 CI 直接跑，且行為與 mocker.spy 一致（委派真實實作、僅旁路記錄）。


async def test_speak_spies_factory_called_once_and_return_flows_into_run_with_retries(
    fake_sdk, monkeypatch, _rl_config, _record_sleep
):
    """A 法：spy 斷言 make_retry_config 在 speak 觸發路徑被呼叫且僅一次，回傳值流入骨幹。"""
    # --- spy #1：工廠（委派真實實作、記錄每次呼叫的回傳 cfg）---------------------
    real_factory = experts.make_retry_config
    factory_returns: list[experts.RetryConfig] = []

    def spy_make_retry_config():
        cfg = real_factory()
        factory_returns.append(cfg)
        return cfg

    monkeypatch.setattr(experts, "make_retry_config", spy_make_retry_config)

    # --- spy #2：骨幹（委派真實實作、攔截傳入的 max_retries 以證回傳值流入）-------
    real_run_with_retries = llm_caller.run_with_retries
    captured: dict = {}

    async def spy_run_with_retries(*args, **kwargs):
        captured["max_retries"] = kwargs.get("max_retries")
        return await real_run_with_retries(*args, **kwargs)

    monkeypatch.setattr(llm_caller, "run_with_retries", spy_run_with_retries)

    # 一次成功發言：不需重試也不觸發 _backoff_delay，確保工廠呼叫數純由 speak 路徑貢獻。
    ok_stream = [
        fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("正常發言")]),
        fake_sdk.ResultMessage(),
    ]
    client = _ScriptedClient(fake_sdk, query_effects=[], stream_msgs=ok_stream)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert text == "正常發言"
    # 1) 觸發路徑確實呼叫工廠，且僅一次（_backoff_delay 維持不內呼工廠，故不會累加）。
    assert len(factory_returns) == 1, f"工廠應僅被呼叫一次，實得 {len(factory_returns)} 次"
    # 2) 工廠回傳值流入骨幹：run_with_retries 收到的 max_retries 即工廠回傳的 cfg.max_retries。
    assert captured["max_retries"] == factory_returns[0].max_retries
    # 自證對應：_rl_config 設 RETRIES=2 → 工廠回傳 2 → 骨幹收到 2（排除假綠的固定值對照）。
    assert factory_returns[0].max_retries == 2
    assert captured["max_retries"] == 2
