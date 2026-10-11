"""QA task #1: timeout abort events must not leak partial text into accepted output."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import types

import pytest
from _repo import REPO_ROOT

from studio import events, experts, providers
from studio.roles import BY_KEY

DANGEROUS_PARTIAL = "任務: 半截輸出 <script>alert(1)</script> 核可"


def _collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


@pytest.fixture
def fake_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class ResultMessage:
        pass

    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


def test_expert_message_partial_text_is_abort_only_metadata():
    normal = events.expert_message(
        "s",
        "qa",
        "QA",
        "Q",
        "一般文字",
        partial_text=DANGEROUS_PARTIAL,
    )
    assert "partial_text" not in normal.payload
    assert "aborted" not in normal.payload

    aborted_without_partial = events.expert_message(
        "s",
        "qa",
        "QA",
        "Q",
        "【系統】逾時",
        aborted=True,
        abort_reason="hard",
    )
    assert aborted_without_partial.payload["aborted"] is True
    assert aborted_without_partial.payload["abort_reason"] == "hard"
    assert "partial_text" not in aborted_without_partial.payload

    aborted = events.expert_message(
        "s",
        "qa",
        "QA",
        "Q",
        "【系統】逾時",
        aborted=True,
        abort_reason="idle",
        partial_text=DANGEROUS_PARTIAL,
    )
    assert aborted.payload["partial_text"] == DANGEROUS_PARTIAL
    assert DANGEROUS_PARTIAL not in aborted.payload["text"]


@pytest.mark.asyncio
async def test_claude_abort_turn_keeps_partial_out_of_returned_note(
    fake_sdk, monkeypatch, tmp_path
):
    class CleanInterruptClient:
        def __init__(self):
            self.interrupted = asyncio.Event()
            self.disconnects = 0

        async def interrupt(self):
            self.interrupted.set()

        async def disconnect(self):
            self.disconnects += 1

        def receive_response(self):
            async def gen():
                await self.interrupted.wait()
                yield fake_sdk.ResultMessage()

            return gen()

    client = CleanInterruptClient()
    monkeypatch.setattr(experts, "_build_client", lambda role, sid, cwd: client)
    expert = experts.Expert(BY_KEY["engineer"], "sess", tmp_path)
    bucket, broadcast = _collect()

    result = await expert._abort_turn(
        experts.ExpertTurnTimeout("idle", DANGEROUS_PARTIAL), broadcast
    )

    assert result.startswith("【系統】")
    assert "逾時中止" in result
    assert "任務:" not in result
    assert "<script>" not in result
    assert "核可" not in result
    assert client.disconnects == 0

    aborted = [ev for ev in bucket if ev.payload.get("aborted")]
    assert len(aborted) == 1
    payload = aborted[0].payload
    assert payload["abort_reason"] == "idle"
    assert payload["partial_text"] == DANGEROUS_PARTIAL
    assert DANGEROUS_PARTIAL not in payload["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expert_cls", [providers.CodexExpert, providers.AntigravityExpert])
async def test_subprocess_provider_timeout_note_keeps_partial_as_metadata_only(
    expert_cls, tmp_path
):
    expert = expert_cls(BY_KEY["qa"], "sess", tmp_path)
    bucket, broadcast = _collect()
    note = "【系統】CLI 發言逾時中止（閒置上限 1 秒），略過本輪發言。"

    result = await expert._system_note(
        note,
        broadcast,
        aborted=True,
        abort_reason="idle",
        partial_text=DANGEROUS_PARTIAL,
    )

    assert result == note
    assert "任務:" not in result
    assert "<script>" not in result
    assert "核可" not in result
    assert len(bucket) == 1
    payload = bucket[0].payload
    assert payload["aborted"] is True
    assert payload["abort_reason"] == "idle"
    assert payload["partial_text"] == DANGEROUS_PARTIAL
    assert payload["text"] == note


@pytest.mark.skipif(shutil.which("node") is None, reason="未安裝 node，略過前端 DOM 驗證")
def test_frontend_abort_render_keeps_partial_out_of_main_text():
    result = subprocess.run(
        ["node", str(REPO_ROOT / "tests" / "frontend_abort_render_contract_test.mjs")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: abort render contract" in result.stdout
