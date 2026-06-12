"""發言層 watchdog（experts.py timeout）單元測試。

沿用 test_experts.py 的注入縫：stream_to_events 以 sys.modules 注入假 claude_agent_sdk、
Expert 以 monkeypatch experts._build_client；全程不需真 SDK、不連線。timeout 取小值
（0.05~0.3s）讓測試快速且不依賴牆鐘精度。
"""

from __future__ import annotations

import asyncio
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


@pytest.fixture
def fake_sdk(monkeypatch):
    """與 test_experts.py 相同的假 claude_agent_sdk 模組。"""
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


def _msgs_then_hang(fake_sdk, texts):
    """先吐出 texts 各一則 AssistantMessage，然後永遠卡住（模擬工具卡死）。"""

    async def gen():
        for t in texts:
            yield fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(t)])
        await asyncio.Event().wait()  # 永不觸發

    return gen()


# --- stream_to_events：idle / hard timeout -------------------------------


async def test_idle_timeout_raises_with_partial_text(fake_sdk):
    role = BY_KEY["engineer"]
    _, broadcast = collect()
    with pytest.raises(experts.ExpertTurnTimeout) as ei:
        await experts.stream_to_events(
            _msgs_then_hang(fake_sdk, ["第一段"]),
            "s",
            role,
            broadcast,
            idle_timeout=0.05,
        )
    assert ei.value.reason == "idle"
    assert ei.value.partial_text == "第一段"


async def test_idle_timeout_resets_on_progress(fake_sdk):
    """每則訊息重置 idle 計時：總時長 > idle_timeout 但訊息間隔都小於它 → 正常完成。"""
    role = BY_KEY["engineer"]
    _, broadcast = collect()

    async def gen():
        for t in ("a", "b", "c", "d"):
            await asyncio.sleep(0.04)  # 4 × 0.04 = 0.16 > idle_timeout
            yield fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock(t)])
        yield fake_sdk.ResultMessage()

    text = await experts.stream_to_events(gen(), "s", role, broadcast, idle_timeout=0.1)
    assert text == "a\nb\nc\nd"


async def test_hard_timeout_caps_total_duration(fake_sdk):
    """有持續進展也擋不住 hard 上限（兜底）。"""
    role = BY_KEY["engineer"]
    _, broadcast = collect()

    async def gen():
        while True:
            await asyncio.sleep(0.02)
            yield fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("x")])

    with pytest.raises(experts.ExpertTurnTimeout) as ei:
        await experts.stream_to_events(
            gen(), "s", role, broadcast, idle_timeout=0.5, hard_timeout=0.1
        )
    assert ei.value.reason == "hard"
    assert "x" in ei.value.partial_text


async def test_no_timeout_is_old_behavior(fake_sdk):
    """兩者皆 None＝原行為：慢訊息也不逾時。"""
    role = BY_KEY["engineer"]
    _, broadcast = collect()

    async def gen():
        await asyncio.sleep(0.1)
        yield fake_sdk.AssistantMessage(content=[fake_sdk.TextBlock("ok")])
        yield fake_sdk.ResultMessage()

    text = await experts.stream_to_events(gen(), "s", role, broadcast)
    assert text == "ok"


# --- Expert.speak：逾時 → _abort_turn 回收 --------------------------------


class _HangingClient:
    """query 後 receive_response 永遠不吐訊息；可注入 interrupt 行為。"""

    def __init__(self, fake_sdk, interrupt_ok: bool):
        self._sdk = fake_sdk
        self._interrupt_ok = interrupt_ok
        self.interrupts = 0
        self.disconnects = 0
        self.connects = 0
        self._interrupted = asyncio.Event()

    async def connect(self):
        self.connects += 1

    async def disconnect(self):
        self.disconnects += 1

    async def query(self, prompt):
        pass

    async def interrupt(self):
        self.interrupts += 1
        if not self._interrupt_ok:
            raise RuntimeError("interrupt failed")
        self._interrupted.set()

    def receive_response(self):
        async def gen():
            if self._interrupted.is_set():
                # interrupt 後的 drain：立即收斂到 turn 邊界
                yield self._sdk.ResultMessage()
                return
            await asyncio.Event().wait()  # 卡死
            yield  # pragma: no cover — 使函式成為 async generator

        return gen()


@pytest.fixture
def _fast_timeouts(monkeypatch):
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)


async def test_speak_timeout_interrupt_recovers(fake_sdk, monkeypatch, _fast_timeouts):
    client = _HangingClient(fake_sdk, interrupt_ok=True)
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: client)
    exp = experts.Expert(BY_KEY["engineer"], "sess", "/tmp/x")
    bucket, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert "逾時中止" in text
    assert client.interrupts == 1
    assert client.disconnects == 0  # 溫和路徑：不需殺行程
    # 系統說明有廣播給 UI，且最後回到 idle 狀態
    assert any("逾時中止" in ev.payload.get("text", "") for ev in bucket)
    assert bucket[-1].payload["status"] == "idle"


async def test_speak_timeout_interrupt_fails_rebuilds_client(fake_sdk, monkeypatch, _fast_timeouts):
    first = _HangingClient(fake_sdk, interrupt_ok=False)
    built = [first]
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: built[-1])
    exp = experts.Expert(BY_KEY["engineer"], "sess", "/tmp/x")

    rebuilt = _HangingClient(fake_sdk, interrupt_ok=True)
    built.append(rebuilt)  # 重建時拿到新 client
    _, broadcast = collect()

    text = await exp.speak("做點事", broadcast)

    assert "已重建" in text
    assert first.interrupts == 1
    assert first.disconnects == 1  # 斷線殺行程
    assert exp._client is rebuilt
    assert exp._connected is False  # 下次 speak 會重新 connect


async def test_speak_default_config_no_timeout_wrapping(fake_sdk, monkeypatch):
    """timeout 設 0（停用）時走原路徑，正常回傳文字。"""
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)

    class _OkClient(_HangingClient):
        def receive_response(self):
            async def gen():
                yield self._sdk.AssistantMessage(content=[self._sdk.TextBlock("完成")])
                yield self._sdk.ResultMessage()

            return gen()

    client = _OkClient(fake_sdk, interrupt_ok=True)
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: client)
    exp = experts.Expert(BY_KEY["engineer"], "sess", "/tmp/x")
    _, broadcast = collect()

    assert await exp.speak("做點事", broadcast) == "完成"
    assert client.interrupts == 0
