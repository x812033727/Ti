"""turn guard：OpenAIExpert 整輪工具迴圈的「總時長」硬上限 + runner cancel 時 killpg 收屍。

回應 autopilot 任務一路撐到 AUTOPILOT_TASK_TIMEOUT（3600s）的根因：OpenAIExpert 過去只有
per-chat 的單次呼叫逾時，缺整輪 speak 的硬上限（Claude 端 stream_to_events 有 hard_timeout、
Codex/Antigravity 有總時長守衛，獨缺 OpenAI 路徑）。逾時被 wait_for 取消時，半途的工具子程序
須經 runner._finalize_proc 的 CancelledError 分支 killpg，避免孤兒程序續燒額度。
"""

from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

from studio import config, providers, runner
from studio.roles import BY_KEY

APPROVAL_HINTS = ("核可", "通過", "approve", "lgtm", "no objection")


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tc(tool_id, name, arguments):
    return SimpleNamespace(id=tool_id, function=SimpleNamespace(name=name, arguments=arguments))


def _collect():
    bucket: list = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


@pytest.mark.asyncio
async def test_openai_turn_guard_aborts_on_whole_loop_hard_timeout(monkeypatch, tmp_path):
    """每步 chat 都在 per-chat 預算內，但多步累加超過 TURN_HARD_TIMEOUT → 整輪硬守衛中止。

    回傳系統逾時 note（不含任何核可關鍵詞，QA 解析自然視為未過），而非無限續跑。
    """
    # per-chat 預算（idle）放寬到不會單次觸發；整輪硬上限收到 0.3s。
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 5.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.3)
    monkeypatch.setattr(config, "OPENAI_MAX_STEPS", 100)

    calls = {"n": 0}

    async def chat(messages, tools, model):
        # 每次呼叫睡一小段（< per-chat 預算）並回一個 tool_call 讓迴圈持續推進，
        # 使「累加時長」而非「單次時長」觸發整輪硬上限。
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return _msg(tool_calls=[_tc(f"c{calls['n']}", "read_file", '{"path": "x"}')])

    expert = providers.OpenAIExpert(BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m")
    _bucket, broadcast = _collect()

    result = await asyncio.wait_for(expert.speak("做點事", broadcast), timeout=10)

    assert "逾時" in result and result.startswith("【系統】")
    assert not any(h.lower() in result.lower() for h in APPROVAL_HINTS)
    # 證明確實是「多步累加」觸發，而非單次 chat 逾時（單次會在第一/二步就被 per-chat 攔下）。
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_openai_turn_completes_under_cap_returns_text(monkeypatch, tmp_path):
    """未逾時的正常路徑仍回傳專家文字（不誤殺）。"""
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 5.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 30.0)

    async def chat(messages, tools, model):
        return _msg(content="這是結論")

    expert = providers.OpenAIExpert(BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m")
    _bucket, broadcast = _collect()

    result = await expert.speak("做點事", broadcast)
    assert result == "這是結論"


@pytest.mark.asyncio
async def test_finalize_proc_killpg_on_cancellation(tmp_path):
    """外層取消 _finalize_proc（CancelledError）時，整組子程序須被 killpg 收屍，不留孤兒。"""
    # 自成 process group leader（start_new_session=True），對齊 runner 兩個 subprocess 分支。
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        "sleep 30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)

    # 用極短外層逾時把 _finalize_proc 卡在 communicate 的點取消掉，模擬整輪 hard-timeout。
    with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
        await asyncio.wait_for(runner._finalize_proc(proc, "sleep", timeout=30), timeout=0.2)

    # 收屍可能非同步完成，給一小段時間讓 SIGKILL 生效。
    for _ in range(50):
        try:
            os.killpg(pgid, 0)  # 探測 group 是否還在
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    # 直屬子程序本身也已回收。
    assert proc.returncode is not None or proc.returncode == -signal.SIGKILL
