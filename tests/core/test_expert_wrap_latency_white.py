"""反向 / 白樣本測試 — expert_wrap 的 NOT 路徑。

僅補 ``tests/core/test_expert_wrap_latency.py`` 之四個反例：

* 非 ``EXPERT_MESSAGE`` 事件不被注入任何 metadata 鍵。
* ``payload`` 非 ``dict``：``speak`` 不炸、metadata 不注入。
* ``speaker`` 與 ``role_key`` 不匹配時不注入。
* ``wrapped.speak`` 拋例外時 ``last_duration_s`` 仍被 ``finally`` 記錄。

每個反例都斷言「注入鍵 **不存在**」(`not in payload`)，而非值為空——這是與黑樣本
(``duration_s == 0.25``) 的判別力所在。本檔 **不重複** 黑樣本的 happy-path 斷言。
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from studio import events, expert_wrap
from studio.roles import BY_KEY

# 與 ``expert_wrap.ExpertTimingProxy._should_annotate`` 配套：wrapper 只在通過
# 該 gate 後才會注入這四個鍵；任何反例都必須維持「四鍵皆不在 payload」。
_INJECTED_KEYS: tuple[str, ...] = ("duration_s", "provider", "model", "role")


# --- 共用 stub：與黑樣本同款，monotonic 耗盡後停在最後值，避免 teardown 干擾 ----


def _controlled_clock(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> None:
    ticks = iter(values)
    last = values[-1]

    def monotonic() -> float:
        return next(ticks, last)

    monkeypatch.setattr(expert_wrap.time, "monotonic", monotonic)


def _run_without_loop(coro: Coroutine[Any, Any, Any]) -> Any:
    """同步驅動不讓出控制權的 coroutine，避免 event loop 消耗受控時鐘序列。"""
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise AssertionError("test coroutine unexpectedly yielded")


async def _collect_speech(
    wrapper: expert_wrap.ExpertTimingProxy,
) -> list[events.StudioEvent]:
    bucket: list[events.StudioEvent] = []

    async def broadcast(event: events.StudioEvent) -> None:
        bucket.append(event)

    await wrapper.speak("hello", broadcast)
    return bucket


# --- 反例 1：非 EXPERT_MESSAGE 不該被注入 ---------------------------------------


class _NonExpertMessageExpert:
    """只發非 ``EXPERT_MESSAGE`` 事件。"""

    role = BY_KEY["engineer"]

    async def speak(self, prompt: str, broadcast) -> str:
        del prompt
        await broadcast(events.expert_status("s", self.role.key, "thinking"))
        await broadcast(events.tool_use("s", self.role.key, "bash", "ls"))
        await broadcast(events.phase_change("s", "implement", "starting"))
        await broadcast(events.human_message("s", "interrupt"))
        return "ok"


def test_non_expert_message_events_receive_no_metadata(monkeypatch):
    wrapper = expert_wrap.with_timing(
        _NonExpertMessageExpert(),
        provider="fake-provider",
        model="fake-model",
    )
    _controlled_clock(monkeypatch, [10.0, 10.75])

    bucket = _run_without_loop(_collect_speech(wrapper))

    assert len(bucket) == 4
    for event in bucket:
        assert event.type != events.EventType.EXPERT_MESSAGE
        for key in _INJECTED_KEYS:
            assert (
                key not in event.payload
            ), f"{key!r} 被注入到 {event.type} 事件 — 只 EXPERT_MESSAGE 該被標註"


# --- 反例 2：payload 非 dict — 不炸、不注入 -----------------------------------


class _BadPayloadExpert:
    """發 EXPERT_MESSAGE 但 payload 是 list（不是 dict）— 破壞性邊界案例。"""

    role = BY_KEY["engineer"]

    async def speak(self, prompt: str, broadcast) -> str:
        del prompt
        sentinel: list[str] = ["untouched"]
        # dataclass 沒做型別強制；payload 接受 list — 代表後端可能餵壞資料。
        await broadcast(
            events.StudioEvent(
                events.EventType.EXPERT_MESSAGE,
                "s",
                sentinel,
            )
        )
        return "ok"


def test_non_dict_payload_does_not_crash_or_get_injected(monkeypatch):
    wrapper = expert_wrap.with_timing(
        _BadPayloadExpert(),
        provider="fake-provider",
        model="fake-model",
    )
    _controlled_clock(monkeypatch, [5.0, 5.42])

    # 不炸：speak() 順利返回
    bucket = _run_without_loop(_collect_speech(wrapper))

    assert bucket[0].type == events.EventType.EXPERT_MESSAGE
    # payload 不該被替換或包裹 — 同一物件參考、同一內容。
    assert bucket[0].payload == ["untouched"]
    # 不注入：在 list sentinel 上，注入鍵本就不該存在。
    for key in _INJECTED_KEYS:
        assert (
            key not in bucket[0].payload
        ), f"{key!r} 竟出現在非 dict payload — wrapper 應該跳過而非亂塞"


# --- 反例 3：speaker 與 role_key 不匹配不注入 -----------------------------------


class _OffSpeakerExpert:
    """EXPERT_MESSAGE 的 speaker 是 ``pm``，但本專家 role 為 ``engineer``。"""

    role = BY_KEY["engineer"]
    _off = BY_KEY["pm"]

    async def speak(self, prompt: str, broadcast) -> str:
        del prompt
        await broadcast(
            events.expert_message(
                "s",
                self._off.key,
                self._off.name,
                self._off.avatar,
                "passing through",
            )
        )
        return "done"


def test_speaker_mismatch_skips_metadata_injection(monkeypatch):
    wrapper = expert_wrap.with_timing(
        _OffSpeakerExpert(),
        provider="fake-provider",
        model="fake-model",
    )
    _controlled_clock(monkeypatch, [20.0, 20.6])

    bucket = _run_without_loop(_collect_speech(wrapper))

    payload = bucket[0].payload
    # 前置條件確認：speaker 確實與 wrapper 的 role_key 不一致。
    assert payload["speaker"] == "pm"
    assert payload["speaker"] != wrapper.wrapped.role.key
    for key in _INJECTED_KEYS:
        assert (
            key not in payload
        ), f"{key!r} 被注入非本人 ({payload['speaker']}) 的訊息 — speaker 不匹配時必須跳過"


# --- 反例 4：wrapped.speak 拋例外 — last_duration_s 仍要記錄 -------------------


class _RaisingExpert:
    """``speak()`` 直接 raise — 驗證 ``finally`` 仍記錄 ``last_duration_s``。"""

    role = BY_KEY["engineer"]

    async def speak(self, prompt: str, broadcast) -> str:
        del prompt, broadcast
        raise RuntimeError("backend blew up")


def test_last_duration_recorded_even_when_speak_raises(monkeypatch):
    wrapper = expert_wrap.with_timing(
        _RaisingExpert(),
        provider="fake-provider",
        model="fake-model",
    )
    # started 在 speak 入口；finally 時再取一次；兩次 monotonic 落差即 elapsed。
    _controlled_clock(monkeypatch, [2.0, 5.0])

    with pytest.raises(RuntimeError, match="backend blew up"):
        _run_without_loop(_collect_speech(wrapper))

    # 反例核心：例外已拋出，但 ``finally`` 仍落地 — last_duration_s 非空。
    assert wrapper.last_duration_s is not None
    assert wrapper.last_duration_s == pytest.approx(3.0)
