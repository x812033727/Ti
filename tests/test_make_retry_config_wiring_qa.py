"""QA wiring 測試（任務 #4）：證 _speak_with_retries 確實「取用」make_retry_config，
且 run_with_retries 實收的退避參數源自 config 當前值（含反向對照排假綠）。

與 test_make_retry_config_qa.py 的分工：
- 後者驗「工廠本身」（存在性、call-time 讀值、lazy backoff、clamp、as_kwargs 形狀）。
- 本檔驗「接線」（單一真實來源真的被走到）——
  1) spy(experts.make_retry_config)：speak() 一輪恰呼叫工廠一次（證確有取用統一入口）。
  2) spy(llm_caller.run_with_retries)：斷言其實收的 max_retries/backoff/sleep 即工廠所供，
     且 backoff 被呼叫時算出的退避＝config 當前值（證 config 值真流入中介層，非只驗有無呼叫）。
  3) 反向對照：改 config 值後重跑，run_with_retries 實收參數隨之變（證非 import 快照／非假綠）。

注入縫沿用 test_experts_ratelimit.py 範式：sys.modules 注入假 claude_agent_sdk、
monkeypatch experts._build_client、零等待 _sleep。全程不需真 SDK、不連線。
"""

from __future__ import annotations

import sys
import types

import pytest

from studio import config, events, experts, llm_caller
from studio.roles import BY_KEY

# --- 共用注入縫（與 test_experts_ratelimit 對齊，本檔自含以免跨檔 fixture 依賴） ----


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


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


class _OkClient:
    """每次 query 正常、receive_response 吐一句正常發言＋ResultMessage（speak 乾淨成功）。"""

    def __init__(self, fake_sdk, text="完成發言"):
        self._sdk = fake_sdk
        self._text = text
        self.queries = 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt):
        self.queries += 1

    def receive_response(self):
        sdk = self._sdk

        async def gen():
            yield sdk.AssistantMessage(content=[sdk.TextBlock(self._text)])
            yield sdk.ResultMessage()

        return gen()


def _make_expert(monkeypatch, client):
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: client)
    return experts.Expert(BY_KEY["engineer"], "sess", "/tmp/x")


@pytest.fixture
def _no_timeout(monkeypatch):
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)


@pytest.fixture
def _no_wait(monkeypatch):
    """零實際等待（本檔走成功路徑通常不退避，仍掛上以防回歸時誤真睡）。"""

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(experts, "_sleep", fake_sleep)


def _spy(monkeypatch, module, name):
    """最小 spy：保留真實實作（call-through），僅記錄 (args, kwargs, result)。

    不引入 pytest-mock；run_with_retries 為 async，wrapper 同步回傳其 coroutine，
    呼叫端 `await` 行為不變。"""
    real = getattr(module, name)
    calls: list[dict] = []

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append({"args": args, "kwargs": kwargs, "result": result})
        return result

    monkeypatch.setattr(module, name, wrapper)
    return calls


def _set_backoff_cfg(monkeypatch, *, retries, base, cap, jitter=0.0):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", retries)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", base)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", cap)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", jitter)


# --- 1) 取用證明：speak() 一輪恰呼叫 make_retry_config 一次 ---------------------


async def test_speak_invokes_make_retry_config_once(fake_sdk, monkeypatch, _no_timeout, _no_wait):
    """spy(make_retry_config)：證 _speak_with_retries 確有走統一入口、且每輪只取一次。"""
    _set_backoff_cfg(monkeypatch, retries=2, base=2.0, cap=60.0)
    spy = _spy(monkeypatch, experts, "make_retry_config")
    client = _OkClient(fake_sdk, text="完成發言")
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert text == "完成發言"  # 行為不變：乾淨成功
    assert len(spy) == 1  # 恰一次（assert_called_once 等價）
    # 工廠回傳真的是統一型別（保留真實邏輯、非被替換）
    assert isinstance(spy[0]["result"], llm_caller.RetryConfig)


# --- 2) 值流入證明：run_with_retries 實收參數＝工廠所供＝config 當前值 -----------


async def test_run_with_retries_receives_config_backed_params(
    fake_sdk, monkeypatch, _no_timeout, _no_wait
):
    """spy(run_with_retries)：斷言實收 max_retries/backoff/sleep 源自 config（非散傳常數）。"""
    _set_backoff_cfg(monkeypatch, retries=3, base=2.0, cap=60.0, jitter=0.0)
    run_calls = _spy(monkeypatch, llm_caller, "run_with_retries")
    client = _OkClient(fake_sdk)
    exp = _make_expert(monkeypatch, client)
    _, broadcast = collect()

    await exp.speak("做點事", broadcast)

    assert len(run_calls) == 1
    kw = run_calls[0]["kwargs"]
    # 退避三參數確由工廠平鋪傳入（cfg.as_kwargs()）
    assert kw["max_retries"] == 3  # ＝ config.EXPERT_RATE_LIMIT_RETRIES 當前值
    assert kw["backoff"] is experts._backoff_delay  # 工廠引用的模組級 lazy 函式
    assert kw["sleep"] is experts._sleep
    # 不只比身份：實際呼叫實收的 backoff，算出的退避＝config base（證值真流入）
    assert kw["backoff"](None, 0) == 2.0  # base 2 × 2^0
    assert kw["backoff"](None, 2) == 8.0  # 2 × 2^2


# --- 3) 反向對照（排假綠）：改 config 值 → run_with_retries 實收參數隨之變 --------


async def test_reverse_control_config_change_flows_into_run_with_retries(
    fake_sdk, monkeypatch, _no_timeout, _no_wait
):
    """同一接線、不同 config：實收 max_retries 與 backoff 算值都跟著變
    → 證走的是 call-time 工廠、單一真實來源，而非載入期快照或寫死常數。"""
    run_calls = _spy(monkeypatch, llm_caller, "run_with_retries")

    # 第一組 config
    _set_backoff_cfg(monkeypatch, retries=1, base=2.0, cap=60.0, jitter=0.0)
    exp1 = _make_expert(monkeypatch, _OkClient(fake_sdk))
    _, broadcast = collect()
    await exp1.speak("第一輪", broadcast)

    kw1 = run_calls[0]["kwargs"]
    assert kw1["max_retries"] == 1
    assert kw1["backoff"](None, 0) == 2.0  # base=2

    # 改 config → 重跑，實收參數須反映新值（反向對照）
    _set_backoff_cfg(monkeypatch, retries=5, base=3.0, cap=60.0, jitter=0.0)
    exp2 = _make_expert(monkeypatch, _OkClient(fake_sdk))
    await exp2.speak("第二輪", broadcast)

    kw2 = run_calls[1]["kwargs"]
    assert kw2["max_retries"] == 5  # 1 → 5
    assert kw2["backoff"](None, 0) == 3.0  # base 2 → 3：config 值確實流入中介層


# --- 4) clamp 經接線後仍生效：負值 config → run_with_retries 實收 0 -------------


async def test_negative_retries_clamped_through_wiring(
    fake_sdk, monkeypatch, _no_timeout, _no_wait
):
    """工廠端 clamp(≥0) 在接線後仍成立：負 config 經 speak → run_with_retries 實收 0。"""
    _set_backoff_cfg(monkeypatch, retries=-5, base=2.0, cap=60.0, jitter=0.0)
    run_calls = _spy(monkeypatch, llm_caller, "run_with_retries")
    exp = _make_expert(monkeypatch, _OkClient(fake_sdk))
    _, broadcast = collect()

    await exp.speak("做點事", broadcast)

    assert run_calls[0]["kwargs"]["max_retries"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
