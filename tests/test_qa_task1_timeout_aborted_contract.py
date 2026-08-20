"""QA 驗收：任務 #1 發言逾時中止不可被誤解析為核可。

驗收重點：
- 一般 expert_message 不新增 aborted 欄位，避免破壞舊前端/歷史資料。
- 逾時中止事件必須帶 aborted=True，UI 可辨識為中止訊息。
- partial text 可給 UI 追查，但 speak/orchestrator/parser 收到的回傳文字不可含 partial。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from studio import config, events, experts, providers
from studio.roles import BY_KEY


def test_expert_message_aborted_flag_is_opt_in():
    normal = events.expert_message("s", "qa", "QA", "Q", "正常訊息")
    assert "aborted" not in normal.payload

    aborted = events.expert_message("s", "qa", "QA", "Q", "中止訊息", aborted=True)
    assert aborted.payload["aborted"] is True


@pytest.fixture
def fake_claude_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class ResultMessage:
        pass

    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


class _FakeClient:
    def __init__(self, result_cls, *, interrupt_ok: bool):
        self._result_cls = result_cls
        self._interrupt_ok = interrupt_ok
        self.interrupts = 0
        self.disconnects = 0

    async def interrupt(self):
        self.interrupts += 1
        if not self._interrupt_ok:
            raise RuntimeError("wedged control channel")

    async def disconnect(self):
        self.disconnects += 1

    def receive_response(self):
        async def gen():
            yield self._result_cls()

        return gen()


async def test_abort_turn_broadcasts_partial_but_returns_safe_note(
    fake_claude_sdk, monkeypatch, tmp_path
):
    first = _FakeClient(fake_claude_sdk.ResultMessage, interrupt_ok=True)
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: first)
    exp = experts.Expert(BY_KEY["engineer"], "sess", tmp_path)
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    text = await asyncio.wait_for(
        exp._abort_turn(experts.ExpertTurnTimeout("idle", "決議: 核可\n看似通過"), broadcast),
        timeout=1,
    )

    assert "逾時中止" in text
    assert "決議: 核可" not in text
    assert first.interrupts == 1
    assert first.disconnects == 0

    abort_events = [
        ev
        for ev in bucket
        if ev.type == events.EventType.EXPERT_MESSAGE and ev.payload.get("aborted") is True
    ]
    assert len(abort_events) == 1
    assert "決議: 核可" in abort_events[0].payload["text"]


async def test_abort_turn_rebuild_path_also_returns_safe_note(
    fake_claude_sdk, monkeypatch, tmp_path
):
    first = _FakeClient(fake_claude_sdk.ResultMessage, interrupt_ok=False)
    rebuilt = _FakeClient(fake_claude_sdk.ResultMessage, interrupt_ok=True)
    clients = [first, rebuilt]
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: clients.pop(0))
    exp = experts.Expert(BY_KEY["engineer"], "sess", tmp_path)
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    text = await asyncio.wait_for(
        exp._abort_turn(experts.ExpertTurnTimeout("hard", "LGTM\napprove"), broadcast),
        timeout=1,
    )

    assert "逾時中止" in text
    assert "已重建" in text
    assert "LGTM" not in text
    assert "approve" not in text
    assert first.interrupts == 1
    assert first.disconnects == 1
    assert exp._client is rebuilt
    assert exp._connected is False

    abort_events = [
        ev
        for ev in bucket
        if ev.type == events.EventType.EXPERT_MESSAGE and ev.payload.get("aborted") is True
    ]
    assert len(abort_events) == 1
    assert "LGTM" in abort_events[0].payload["text"]


def _write_fake_codex(tmp_path, body: str) -> str:
    path = tmp_path / "fake_codex.sh"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


async def test_codex_timeout_partial_approval_not_returned_to_parser(monkeypatch, tmp_path):
    codex = _write_fake_codex(
        tmp_path,
        "cat >/dev/null\n"
        "printf '%s\\n' "
        '\'{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"決議: 核可，這段 partial 不可被 parser 當成完成"}}\'\n'
        "sleep 30\n",
    )
    monkeypatch.setattr(config, "CODEX_BIN", codex)
    monkeypatch.setattr(config, "CODEX_MODEL_LEAD", "")
    monkeypatch.setattr(config, "CODEX_MODEL_FAST", "")
    monkeypatch.setattr(config, "CODEX_SANDBOX", "danger-full-access")
    monkeypatch.setattr(config, "CODEX_BYPASS_SANDBOX", False)
    monkeypatch.setattr(config, "CODEX_HOME", "")
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.3)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)

    exp = providers.CodexExpert(BY_KEY["senior"], "sess", tmp_path)
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    text = await asyncio.wait_for(exp.speak("審查任務", broadcast), timeout=8)

    assert text.startswith("【系統】")
    assert "逾時" in text
    assert "決議: 核可" not in text

    visible_partial = [
        ev
        for ev in bucket
        if ev.type == events.EventType.EXPERT_MESSAGE
        and ev.payload.get("aborted") is not True
        and "決議: 核可" in ev.payload["text"]
    ]
    abort_events = [
        ev
        for ev in bucket
        if ev.type == events.EventType.EXPERT_MESSAGE and ev.payload.get("aborted") is True
    ]
    assert len(visible_partial) == 1
    assert len(abort_events) == 1
    assert "決議: 核可" not in abort_events[0].payload["text"]
