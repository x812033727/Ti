"""Autopilot provider unavailable handling.

契約（自「額度感知節奏」後改版）：provider 不可用時把任務退回 pending 即 return，
**不再 _pause() 寫 pause 檔永久暫停等人工 resume**——下一輪主迴圈的額度閘門
（provider_quota.gate）會睡到額度重置後自動續跑。_pause() 保留給重佈失敗分支。
"""

from __future__ import annotations

import pytest

from studio import autopilot


@pytest.mark.asyncio
async def test_run_one_task_requeues_on_provider_unavailable(monkeypatch, tmp_path):
    """Provider 額度/可用性問題應把任務退回 pending 且不寫 pause 檔（長跑不間斷）。"""
    clone = tmp_path / "clone"
    clone.mkdir()
    statuses = []
    pauses = []

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _requirement):
            return {
                "completed": False,
                "followups": [],
                "followup_items": [],
                "core_changes": [],
                "provider_unavailable": "codex",
            }

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", FakeSession)
    monkeypatch.setattr(autopilot.history, "start_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autopilot.history, "record_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autopilot.history, "finish_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        autopilot.backlog,
        "set_status",
        lambda task_id, status, **kw: statuses.append((task_id, status, kw)),
    )
    monkeypatch.setattr(autopilot.backlog, "add_items", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(autopilot.backlog, "add_many", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(autopilot, "_pause", lambda reason: pauses.append(reason))

    await autopilot.run_one_task({"id": 7, "title": "觸發 Codex usage limit"})

    assert statuses[0][0:2] == (7, "in_progress")
    assert statuses[0][2]["session_id"].startswith("ap")
    # 退回 pending（帶原因 note），由下一輪額度閘門自然睡眠後重跑
    assert statuses[-1] == (7, "pending", {"note": "codex provider unavailable"})
    # 關鍵改版斷言：不得呼叫 _pause（不寫 pause 檔、不永久暫停）
    assert pauses == []
