"""主動通知 webhook（功能第五輪 F2）：studio/notify + autopilot 三個觸發點。

守護不變量：
- 未設 TI_NOTIFY_WEBHOOK（預設）→ send/send_bg 完全 no-op、零網路;
- send POST JSON（source/kind/title+extra）到設定端點;任何網路失敗吞掉回 False;
- 觸發點:閘門重試用罄 failed、討論未收斂用罄 failed、主迴圈心跳停滯告警——
  皆走 send_bg(零阻塞),退回 pending 的中間重試不通知。
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from studio import autopilot, backlog, config, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    return tmp_path


def _capture_urlopen(monkeypatch, *, boom=False):
    calls: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        if boom:
            raise OSError("connection refused")
        calls.append(
            {
                "url": req.full_url,
                "body": json.loads(req.data.decode("utf-8")),
                "method": req.get_method(),
            }
        )
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def test_send_noop_without_webhook(monkeypatch):
    calls = _capture_urlopen(monkeypatch)
    assert notify.send("task_failed", "x") is False
    notify.send_bg("task_failed", "x")
    assert not calls, "未設 webhook 必須零網路"


def test_send_posts_json_payload(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    calls = _capture_urlopen(monkeypatch)
    assert notify.send("loop_stall", "主迴圈停滯", idle_for=900) is True
    assert calls[0]["url"] == "https://hook.example/ti" and calls[0]["method"] == "POST"
    assert calls[0]["body"] == {
        "source": "ti",
        "kind": "loop_stall",
        "title": "主迴圈停滯",
        "idle_for": 900,
    }


def test_send_swallows_network_failure(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    _capture_urlopen(monkeypatch, boom=True)
    assert notify.send("task_failed", "x") is False  # 不拋即通過


def _spy_send_bg(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        autopilot.notify, "send_bg", lambda kind, title, **kw: sent.append((kind, title, kw))
    )
    return sent


def test_gate_failure_exhaustion_notifies(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_TASK_MAX_ATTEMPTS", 2)
    sent = _spy_send_bg(monkeypatch)
    t = backlog.add("會失敗的任務")
    backlog.set_status(t["id"], "in_progress", attempts=0)
    autopilot._handle_gate_failure({**t, "attempts": 0}, "test", "第一次失敗")
    assert not sent, "中間重試（退回 pending）不通知"
    autopilot._handle_gate_failure({**t, "attempts": 1}, "test", "第二次失敗")
    assert [s[0] for s in sent] == ["task_failed", "ci_failed"]
    assert sent[0][2]["task_id"] == t["id"]


def test_discussion_exhaustion_notifies(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 1)
    sent = _spy_send_bg(monkeypatch)
    t = backlog.add("討論不收斂的任務")
    backlog.set_status(t["id"], "in_progress")
    autopilot._handle_discussion_incomplete({**t, "attempts": 0})
    assert [s[0] for s in sent] == ["task_failed"]


@pytest.mark.asyncio
async def test_loop_stall_notifies_once(monkeypatch):
    import asyncio

    monkeypatch.setattr(config, "AUTOPILOT_LOOP_STALL_S", 100)
    monkeypatch.setattr(autopilot, "_task_running", False)
    monkeypatch.setattr(autopilot, "_loop_tick_at", time.time() - 500)
    sent = _spy_send_bg(monkeypatch)
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await autopilot._loop_monitor()
    assert [s[0] for s in sent] == ["loop_stall"], "同一停滯期告警+通知都只發一次"
