"""Expert wrapper utilities."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import events

Broadcast = Callable[[Any], Awaitable[None]]


class ExpertTimingProxy:
    """Proxy that annotates expert_message events with elapsed speak time.

    ``duration_s`` is wall-clock time measured with ``time.monotonic()`` from the
    start of the current ``speak()`` call. It includes retry backoff and any other
    waiting inside the backend speak implementation. For streaming backends, every
    EXPERT_MESSAGE gets the cumulative duration so the last message approximates
    the full turn duration.

    The payload mutation relies on the current broadcast -> history recording
    order: events are annotated before the caller records or forwards them.
    """

    def __init__(
        self,
        wrapped: Any,
        *,
        provider: str | None = None,
        model: str | None = None,
    ):
        self.wrapped = wrapped
        self._timing_provider = provider
        self._timing_model = model
        self.last_duration_s: float | None = None

    @property
    def __class__(self):  # type: ignore[override]
        return self.wrapped.__class__

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)

    async def speak(self, prompt: str, broadcast: Broadcast) -> str:
        started = time.monotonic()

        async def timed_broadcast(event: Any) -> None:
            if self._should_annotate(event):
                payload = event.payload
                payload["duration_s"] = time.monotonic() - started
                payload["provider"] = self._provider_name()
                payload["model"] = self._model_name()
                payload["role"] = self._role_key()
            await broadcast(event)

        try:
            return await self.wrapped.speak(prompt, timed_broadcast)
        finally:
            self.last_duration_s = time.monotonic() - started

    def _should_annotate(self, event: Any) -> bool:
        if getattr(event, "type", None) != events.EventType.EXPERT_MESSAGE:
            return False
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            return False
        speaker = payload.get("speaker")
        role_key = self._role_key()
        return not speaker or not role_key or speaker == role_key

    def _provider_name(self) -> str:
        if self._timing_provider is not None:
            return self._timing_provider
        return str(
            getattr(self.wrapped, "provider", None)
            or getattr(self.wrapped, "_provider", None)
            or ""
        )

    def _model_name(self) -> str:
        if self._timing_model is not None:
            return self._timing_model
        effective_model = getattr(self.wrapped, "effective_model", None)
        if callable(effective_model):
            return str(effective_model() or "")
        return str(
            getattr(self.wrapped, "model", None)
            or getattr(self.wrapped, "_model", None)
            or getattr(self.wrapped, "_model_override", None)
            or ""
        )

    def _role_key(self) -> str:
        role = getattr(self.wrapped, "role", None)
        return str(getattr(role, "key", None) or "")


def with_timing(
    expert: Any,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ExpertTimingProxy:
    """Wrap an expert with timing annotation, leaving existing attrs proxied."""
    if isinstance(expert, ExpertTimingProxy):
        return expert
    return ExpertTimingProxy(expert, provider=provider, model=model)
