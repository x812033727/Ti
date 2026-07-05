from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from studio import events, expert_wrap
from studio.roles import BY_KEY


class _ScriptedExpert:
    def __init__(self, messages: list[str]):
        self.role = BY_KEY["engineer"]
        self.messages = messages

    async def speak(self, prompt: str, broadcast) -> str:
        del prompt
        for idx, text in enumerate(self.messages):
            await broadcast(
                events.expert_message(
                    "s",
                    self.role.key,
                    self.role.name,
                    self.role.avatar,
                    text,
                    streaming=idx < len(self.messages) - 1,
                    final=idx == len(self.messages) - 1,
                )
            )
        return "".join(self.messages)


def _controlled_clock(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> None:
    ticks = iter(values)
    last = values[-1]

    def monotonic() -> float:
        return next(ticks, last)

    monkeypatch.setattr(expert_wrap.time, "monotonic", monotonic)


def _run_without_loop(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise AssertionError("test coroutine unexpectedly yielded")


async def _collect_speech(wrapper: expert_wrap.ExpertTimingProxy) -> list[events.StudioEvent]:
    bucket: list[events.StudioEvent] = []

    async def broadcast(event: events.StudioEvent) -> None:
        bucket.append(event)

    await wrapper.speak("hello", broadcast)
    return bucket


def test_timing_injects_metadata_with_deterministic_duration(monkeypatch):
    wrapper = expert_wrap.with_timing(
        _ScriptedExpert(["done"]),
        provider="fake-provider",
        model="fake-model",
    )
    _controlled_clock(monkeypatch, [10.0, 10.25, 10.5])

    bucket = _run_without_loop(_collect_speech(wrapper))

    payload = bucket[0].payload
    assert payload["duration_s"] == 0.25
    assert payload["provider"] == "fake-provider"
    assert payload["model"] == "fake-model"
    assert payload["role"] == "engineer"
    assert wrapper.last_duration_s == 0.5


def test_streaming_expert_messages_get_monotonic_cumulative_duration(monkeypatch):
    wrapper = expert_wrap.with_timing(
        _ScriptedExpert(["a", "b", "c"]),
        provider="fake-provider",
        model="fake-model",
    )
    _controlled_clock(monkeypatch, [100.0, 100.1, 100.4, 100.9, 101.2])

    bucket = _run_without_loop(_collect_speech(wrapper))

    durations = [
        event.payload["duration_s"]
        for event in bucket
        if event.type == events.EventType.EXPERT_MESSAGE
    ]
    assert durations == [pytest.approx(0.1), pytest.approx(0.4), pytest.approx(0.9)]
    assert durations == sorted(durations)
    assert wrapper.last_duration_s == pytest.approx(1.2)


def test_with_timing_is_idempotent_for_existing_proxy():
    wrapped = expert_wrap.with_timing(
        _ScriptedExpert(["done"]),
        provider="fake-provider",
        model="fake-model",
    )

    assert expert_wrap.with_timing(wrapped, provider="other", model="other") is wrapped
