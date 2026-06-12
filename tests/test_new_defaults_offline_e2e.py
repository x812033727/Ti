"""新預設學習組合的離線端到端守門測試。

學習機制自 2026-06 起預設開啟（huddle／notes／lessons／reflexion／客觀閘門／自我精修；
critic 維持 opt-in）。本測試把這個組合顯式 pin 上（防測試環境 env 漂移），用離線假專家
跑完整 session，斷言流程仍能順利完成——守住「預設組合彼此相容、不會把 happy path 弄壞」。

循序模式（自測/Demo 只需 host python，不依賴 pytest 可執行）、sandbox/git 關（不需 bwrap）。
"""

from __future__ import annotations

import pytest

from studio import config, events, workspace
from studio.fake_experts import build_fake_experts
from studio.orchestrator import StudioSession


@pytest.mark.asyncio
async def test_new_default_learning_combo_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")
    # 新預設學習組合（與 studio/config.py 預設一致）
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    monkeypatch.setattr(config, "NOTES_MAX_CHARS", 6000)
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", True)
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "1")
    monkeypatch.setattr(config, "SELF_REFINE_ITERS", 1)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "KNOWLEDGE_ENABLED", True)
    monkeypatch.setattr(config, "CLARIFY_ENABLED", True)  # 無 queue → 自動跳過

    sid = "newdefaults"
    cwd = workspace.create_workspace(sid)
    bucket: list[events.StudioEvent] = []

    async def bc(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    experts = build_fake_experts(sid, cwd, "四則運算 CLI")
    session = StudioSession(sid, bc, experts=experts, cwd=cwd)
    result = await session.run("做一個四則運算 CLI")

    assert result["completed"] is True, "新預設組合下離線 happy path 必須能跑完"
    done = [e for e in bucket if e.type == events.EventType.DONE][-1]
    assert done.payload["completed"] is True
    # 開關開著但 happy path 不該觸發失敗型機制
    phases = [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "卡關討論" not in phases
    # NOTES 照常累積（任務摘要）
    assert "任務 #1 完成" in workspace.read_notes(sid)
