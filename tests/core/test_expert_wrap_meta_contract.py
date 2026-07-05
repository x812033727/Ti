"""meta 鍵存在契約守護 — expert_wrap 注入的四鍵 payload 層契約。

與 ``test_expert_wrap_latency.py``（黑：斷言 **值** 為確定值域）和
``test_expert_wrap_latency_white.py``（白：斷言注入鍵 **不存在**）互補，本檔專守
**契約** 而非值：

* 注入後 payload **必含** ``duration_s`` / ``provider`` / ``model`` / ``role`` 四鍵
  （用 ``key in payload`` 直接斷言鍵存在）。教訓：實作端 ``payload.get(k) or {}``
  這類容錯讀法**不算契約**——若有人刪掉任一 ``payload[...] = ...`` 注入行，
  本檔必紅。這裡刻意不斷言值，只鎖「鍵存在」，避免與黑樣本重疊。
* 缺省容忍：模擬「舊事件」——payload 天生沒有這四鍵（本功能上線前錄下的事件）。
  消費端以 ``.get()`` 讀取時必須回 ``None`` 而不炸；契約允許鍵缺席被優雅降級。

本檔 **不** 重測 duration 值域、串流遞增、no-op 反例（那些屬黑/白樣本）。
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from studio import events, expert_wrap
from studio.roles import BY_KEY

# 契約鎖定的四鍵：wrapper 通過 ``_should_annotate`` gate 後必須逐一注入。
# 任一注入行被移除 → ``test_injected_payload_contains_all_meta_keys`` 必紅。
_CONTRACT_KEYS: tuple[str, ...] = ("duration_s", "provider", "model", "role")


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


class _ScriptedExpert:
    """發單則 EXPERT_MESSAGE 的最小專家。"""

    role = BY_KEY["engineer"]

    async def speak(self, prompt: str, broadcast) -> str:
        del prompt
        await broadcast(
            events.expert_message(
                "s",
                self.role.key,
                self.role.name,
                self.role.avatar,
                "hello",
            )
        )
        return "hello"


async def _speak_once(wrapper: expert_wrap.ExpertTimingProxy) -> events.StudioEvent:
    bucket: list[events.StudioEvent] = []

    async def broadcast(event: events.StudioEvent) -> None:
        bucket.append(event)

    await wrapper.speak("hi", broadcast)
    assert len(bucket) == 1
    return bucket[0]


# --- 契約 1：注入後四鍵皆存在（鍵存在，非值） ---------------------------------


def test_injected_payload_contains_all_meta_keys(monkeypatch):
    wrapper = expert_wrap.with_timing(
        _ScriptedExpert(),
        provider="fake-provider",
        model="fake-model",
    )
    _controlled_clock(monkeypatch, [1.0, 1.5])

    event = _run_without_loop(_speak_once(wrapper))
    payload = event.payload

    # 鍵存在契約：逐一以 ``in`` 斷言，任一注入行被移除即紅。
    # 刻意不比對值——值域由黑樣本 (test_expert_wrap_latency.py) 守。
    missing = [key for key in _CONTRACT_KEYS if key not in payload]
    assert not missing, f"注入後 payload 缺少契約鍵 {missing} — 有人動了注入行？"

    # 補強：``keys()`` 是 payload 的真實鍵集合（防「__contains__ 被覆寫」的假綠）。
    for key in _CONTRACT_KEYS:
        assert key in payload.keys()


# --- 契約 2：缺省容忍 — 舊事件無這四鍵，消費端 .get() 不炸 ----------------------


def test_legacy_payload_without_meta_keys_is_tolerated():
    """模擬本功能上線前錄下的舊事件：payload 從未被注入四鍵。"""
    legacy = events.expert_message(
        "s",
        "engineer",
        "工程師",
        "🧑‍💻",
        "old recorded message",
    )

    # 前置條件：舊事件確實沒有任何契約鍵。
    for key in _CONTRACT_KEYS:
        assert key not in legacy.payload

    # 契約：消費端以 ``.get()`` 讀缺席鍵 → 回 None，不拋 KeyError。
    for key in _CONTRACT_KEYS:
        assert legacy.payload.get(key) is None

    # 附帶預設值的容忍讀法（史料聚合常見）也不該炸。
    assert legacy.payload.get("duration_s", 0.0) == 0.0
    assert legacy.payload.get("provider", "") == ""


# --- 契約 3：未通過 gate 時不「半注入」— 四鍵全有或全無 ------------------------


class _MismatchExpert:
    """speaker 與 role 不一致，wrapper 不應注入任何契約鍵。"""

    role = BY_KEY["engineer"]

    async def speak(self, prompt: str, broadcast) -> str:
        del prompt
        pm = BY_KEY["pm"]
        await broadcast(events.expert_message("s", pm.key, pm.name, pm.avatar, "not mine"))
        return "done"


def test_meta_keys_are_injected_all_or_nothing(monkeypatch):
    """契約不變式：契約鍵集合在單一 payload 上只會「全存在」或「全缺席」，
    不會出現部分注入的破碎狀態。"""
    # 全存在案例
    good = expert_wrap.with_timing(_ScriptedExpert(), provider="p", model="m")
    _controlled_clock(monkeypatch, [3.0, 3.2])
    good_payload = _run_without_loop(_speak_once(good)).payload
    present_good = [k for k in _CONTRACT_KEYS if k in good_payload]

    # 全缺席案例（speaker 不匹配，未通過 gate）
    bad = expert_wrap.with_timing(_MismatchExpert(), provider="p", model="m")
    _controlled_clock(monkeypatch, [4.0, 4.3])

    bucket: list[events.StudioEvent] = []

    async def _drive() -> None:
        async def broadcast(event: events.StudioEvent) -> None:
            bucket.append(event)

        await bad.speak("hi", broadcast)

    _run_without_loop(_drive())
    bad_payload = bucket[0].payload
    present_bad = [k for k in _CONTRACT_KEYS if k in bad_payload]

    assert present_good == list(_CONTRACT_KEYS), "通過 gate 應注入全部契約鍵"
    assert present_bad == [], "未通過 gate 不該有任何契約鍵（禁止半注入）"
