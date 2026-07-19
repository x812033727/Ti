"""發言層回收路徑的逾時綁定（issue #286 根因修復）單元測試。

背景：`Expert.stop()` 的 disconnect()、`_abort_turn` 的 interrupt()／disconnect() 原本
未加逾時，一旦 stdio 控制通道 wedged（子程序卡 ep_poll、零 CPU）就永久卡死；且這些呼叫
落在 session.run 收尾與外層任務逾時取消的清理路徑上，使連 3600s backstop 都無法收斂，只有
人工 systemctl restart 才解。此檔驗證：控制通道卡死時，回收在 _CTRL_TIMEOUT 內收斂並改走
best-effort SIGKILL＋重建，發言層 watchdog 因此能可靠回收卡住的子程序。

沿用 test_experts_timeout.py 的注入縫：以 sys.modules 注入假 claude_agent_sdk、Expert 以
monkeypatch experts._build_client 注入假 client，全程不需真 SDK；_CTRL_TIMEOUT 取小值讓
測試快速且不依賴牆鐘精度。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from studio import events, experts, runner
from studio.roles import BY_KEY


@pytest.fixture
def fake_sdk(monkeypatch):
    """與 test_experts_timeout.py 相同的假 claude_agent_sdk 模組（_abort_turn 會 import
    ResultMessage）。"""
    mod = types.ModuleType("claude_agent_sdk")

    class ResultMessage:
        pass

    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


class _CtrlClient:
    """可個別設定 interrupt()／disconnect() 為「立即成功」或「永遠卡住」的假 client。

    receive_response 立即收斂到 ResultMessage（drain 不是本檔重點）；_transport._process
    預設帶一個假 pid，供 best-effort kill 兜底路徑取用。
    """

    def __init__(self, *, interrupt_hang: bool = False, disconnect_hang: bool = False):
        self._interrupt_hang = interrupt_hang
        self._disconnect_hang = disconnect_hang
        self.interrupts = 0
        self.disconnects = 0
        # SDK 內部形狀：client._transport._process（best-effort kill 會 getattr 取用）
        self._transport = types.SimpleNamespace(_process=types.SimpleNamespace(pid=-999999))

    async def connect(self):
        pass

    async def query(self, prompt):
        pass

    async def interrupt(self):
        self.interrupts += 1
        if self._interrupt_hang:
            await asyncio.Event().wait()  # 永不返回：模擬 wedged 控制通道

    async def disconnect(self):
        self.disconnects += 1
        if self._disconnect_hang:
            await asyncio.Event().wait()  # 永不返回

    def receive_response(self):
        async def gen():
            from claude_agent_sdk import ResultMessage

            yield ResultMessage()

        return gen()


def _make_expert(monkeypatch, client, *, ctrl_timeout: float = 0.05):
    """建一位注入 client 的 Expert，並把 _CTRL_TIMEOUT 調小以加速逾時。"""
    monkeypatch.setattr(experts, "_CTRL_TIMEOUT", ctrl_timeout)
    built = [client]
    monkeypatch.setattr(experts, "_build_client", lambda *a, **k: built[-1])
    exp = experts.Expert(BY_KEY["engineer"], "sess", "/tmp/x")
    return exp, built


# --- Expert.stop()：kill-first 後 disconnect 卡死仍須在 _CTRL_TIMEOUT 內收斂 ----------


async def test_stop_bounded_when_disconnect_hangs(monkeypatch):
    client = _CtrlClient(disconnect_hang=True)
    exp, _ = _make_expert(monkeypatch, client)
    exp._connected = True

    calls: list[tuple[str, bool]] = []
    reentrant_stops: list[asyncio.Task] = []
    original_disconnect = client.disconnect

    def kill_first_probe():
        calls.append(("kill", exp._connected))
        reentrant_stops.append(asyncio.create_task(exp.stop()))

    async def disconnect_probe():
        calls.append(("disconnect", exp._connected))
        await original_disconnect()

    monkeypatch.setattr(exp, "_best_effort_kill_subprocess", kill_first_probe)
    monkeypatch.setattr(client, "disconnect", disconnect_probe)

    # 未綁定時這裡會永遠卡住；wait_for(2) 是測試保險，真正的收斂來自 stop() 內的 _CTRL_TIMEOUT。
    await asyncio.wait_for(exp.stop(), timeout=2)
    if reentrant_stops:
        await asyncio.gather(*reentrant_stops)

    assert client.disconnects == 1
    assert calls == [
        ("kill", False),
        ("disconnect", False),
    ], "stop() 必須先標離線、先 SIGKILL，再進 disconnect；重入不得重複斷線"
    assert exp._connected is False


async def test_stop_ok_when_disconnect_fast(monkeypatch):
    """正常路徑也先 kill SDK 子程序群，再讓 disconnect 收斂 client 狀態。"""
    client = _CtrlClient()
    exp, _ = _make_expert(monkeypatch, client)
    exp._connected = True

    calls: list[str] = []
    original_disconnect = client.disconnect

    async def disconnect_probe():
        calls.append("disconnect")
        await original_disconnect()

    monkeypatch.setattr(exp, "_best_effort_kill_subprocess", lambda: calls.append("kill"))
    monkeypatch.setattr(client, "disconnect", disconnect_probe)

    await asyncio.wait_for(exp.stop(), timeout=2)

    assert client.disconnects == 1
    assert calls == ["kill", "disconnect"]
    assert exp._connected is False


async def test_stop_bounded_when_kill_noop_and_disconnect_hangs(monkeypatch):
    """SDK 私有 transport 形狀取不到時，stop() 仍須靠 _CTRL_TIMEOUT 有界返回。"""
    client = _CtrlClient(disconnect_hang=True)
    del client._transport
    exp, _ = _make_expert(monkeypatch, client)
    exp._connected = True

    killed: list[object] = []
    monkeypatch.setattr(runner, "kill_process_group", lambda proc: killed.append(proc))

    await asyncio.wait_for(exp.stop(), timeout=2)

    assert killed == []
    assert client.disconnects == 1
    assert exp._connected is False


async def test_stop_bounded_when_disconnect_swallows_cancellation(monkeypatch):
    """disconnect 吞掉 CancelledError 仍不返回時，stop() 不得等它取消完成。"""
    client = _CtrlClient()
    exp, _ = _make_expert(monkeypatch, client)
    exp._connected = True

    cancel_seen = asyncio.Event()
    release_disconnect = asyncio.Event()
    disconnect_done = asyncio.Event()

    async def disconnect_probe():
        client.disconnects += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            await release_disconnect.wait()
        finally:
            disconnect_done.set()

    monkeypatch.setattr(exp, "_best_effort_kill_subprocess", lambda: None)
    monkeypatch.setattr(client, "disconnect", disconnect_probe)

    stop_task = asyncio.create_task(exp.stop())
    done, pending = await asyncio.wait({stop_task}, timeout=1)
    if pending:
        release_disconnect.set()
        await asyncio.wait_for(stop_task, timeout=1)
        pytest.fail("stop() 等待 disconnect 取消完成，未有界返回")

    assert done == {stop_task}
    await stop_task
    await asyncio.wait_for(cancel_seen.wait(), timeout=1)

    release_disconnect.set()
    await asyncio.wait_for(disconnect_done.wait(), timeout=1)
    assert client.disconnects == 1
    assert exp._connected is False


# --- _abort_turn：interrupt 卡死須在 ~2×_CTRL_TIMEOUT 內收斂、斷線重建 ------------


async def test_abort_turn_bounded_when_interrupt_hangs(fake_sdk, monkeypatch):
    """interrupt() 卡在 wedged 通道：逾時後落斷線分支，disconnect 成功即重建 client。"""
    first = _CtrlClient(interrupt_hang=True)  # interrupt 卡死、disconnect 正常
    exp, built = _make_expert(monkeypatch, first)
    rebuilt = _CtrlClient()
    built.append(rebuilt)  # _new_client 重建時拿到新 client
    _, broadcast = collect()

    exc = experts.ExpertTurnTimeout("idle", "逾時前片段")
    note = await asyncio.wait_for(exp._abort_turn(exc, broadcast), timeout=2)

    assert first.interrupts == 1
    assert first.disconnects == 1  # interrupt 逾時 → 落斷線分支
    assert "已重建" in note
    assert "逾時前片段" in note
    assert exp._client is rebuilt
    assert exp._connected is False


async def test_abort_turn_bounded_when_interrupt_and_disconnect_hang(fake_sdk, monkeypatch):
    """interrupt() 與 disconnect() 皆卡死：兩段各逾時後走 best-effort kill 再重建，全程有界。"""
    first = _CtrlClient(interrupt_hang=True, disconnect_hang=True)
    exp, built = _make_expert(monkeypatch, first)
    rebuilt = _CtrlClient()
    built.append(rebuilt)

    killed: list[bool] = []
    monkeypatch.setattr(exp, "_best_effort_kill_subprocess", lambda: killed.append(True))
    _, broadcast = collect()

    exc = experts.ExpertTurnTimeout("idle", "")
    note = await asyncio.wait_for(exp._abort_turn(exc, broadcast), timeout=2)

    assert first.interrupts == 1
    assert first.disconnects == 1
    assert killed == [True], "disconnect 也卡死應走 best-effort SIGKILL 兜底"
    assert "已重建" in note
    assert exp._client is rebuilt


# --- _best_effort_kill_subprocess：形狀缺失時靜默降級，不得拋 ----------------------


async def test_best_effort_kill_survives_missing_transport(monkeypatch):
    class _NoTransport:
        pass

    exp, _ = _make_expert(monkeypatch, _NoTransport())

    called: list = []
    monkeypatch.setattr(runner, "kill_process_group", lambda proc: called.append(proc))

    exp._best_effort_kill_subprocess()  # 不得拋

    assert called == [], "無 _transport 應靜默 no-op，不呼叫 kill"


async def test_best_effort_kill_calls_runner_when_process_present(monkeypatch):
    client = _CtrlClient()  # 帶 _transport._process(pid=-999999)
    exp, _ = _make_expert(monkeypatch, client)

    called: list = []
    monkeypatch.setattr(runner, "kill_process_group", lambda proc: called.append(proc))
    # 2026-07-19 自殺防護後,killpg 只在「子程序與本行程異 group」時走——測試明確給異組。
    import os as _os

    monkeypatch.setattr(_os, "getpgid", lambda pid: 999 if pid == -999999 else 111)

    exp._best_effort_kill_subprocess()

    assert len(called) == 1
    assert called[0] is client._transport._process


async def test_best_effort_kill_swallows_runner_errors(monkeypatch):
    """runner.kill_process_group 拋例外時仍須被吞掉（兜底殺程序不得影響回收流程）。"""
    client = _CtrlClient()
    exp, _ = _make_expert(monkeypatch, client)

    def _boom(proc):
        raise RuntimeError("killpg failed")

    monkeypatch.setattr(runner, "kill_process_group", _boom)

    exp._best_effort_kill_subprocess()  # 不得拋
