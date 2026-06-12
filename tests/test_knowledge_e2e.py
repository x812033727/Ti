"""端到端驗證：知識沉澱（docs/RESEARCH.md）在真實 orchestrator 流程的接線。
（設計決策的沉澱/注入已移交 ADR 模組，見 tests/core 的 ADR 專測。）

沿用 tests/test_lessons_e2e.py 的 StubExpert 模式，加入研究員與架構師：
  第 1 場：調研結論與 `設計決策:` 行落盤 docs/ → 第 2 場（同一 workspace，模擬專案模式）
  研究員 prompt 收到既有調研、PM 拆解 prompt 收到既有設計決策。
"""

from __future__ import annotations

import pytest

from studio import config, events, workspace
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _base_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(config, "KNOWLEDGE_ENABLED", True)
    monkeypatch.setattr(config, "KNOWLEDGE_MAX_CHARS", 4000)


async def _run_session(sid: str, workspace_id: str, research_text: str) -> dict:
    async def broadcast(ev: events.StudioEvent) -> None:
        pass

    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: 實作核心功能", "決議: 完成", "檢討：無"]),
        "engineer": StubExpert(BY_KEY["engineer"], ["做好了", "看起來可行"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可", "沒有疑慮"]),
        "researcher": StubExpert(BY_KEY["researcher"], [research_text]),
        # 第 1 次發言＝提案、第 2 次＝定案（`設計決策:` 行才會被沉澱）。
        "architect": StubExpert(
            BY_KEY["architect"],
            ["提案：走輕量 web 架構", "設計決策: 後端用 FastAPI\n設計決策: 儲存用 SQLite"],
        ),
    }
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(workspace_id),
        workspace_id=workspace_id,
    )
    await session.run("做一個記帳小工具")
    return experts


@pytest.mark.asyncio
async def test_knowledge_persists_then_injected_next_session():
    """第 1 場沉澱調研＋決策 → 第 2 場（同 workspace）開場注入。"""
    workspace.create_workspace("proj-k1")
    await _run_session("s1", "proj-k1", "重點: 記帳類常用 double-entry 模型\n建議: 金額用整數分")

    # 落盤驗證：屬交付物、出現在檔案面板
    files = workspace.list_files("proj-k1")
    assert "docs/RESEARCH.md" in files
    assert "double-entry" in workspace.read_doc_tail("proj-k1", "RESEARCH.md", 4000)

    # 第 2 場：不清空 workspace（模擬專案模式固定 workspace）
    experts2 = await _run_session("s2", "proj-k1", "重點: 第二場新發現")
    researcher_prompt = experts2["researcher"].prompts[0]
    assert "既有調研" in researcher_prompt
    assert "double-entry" in researcher_prompt


@pytest.mark.asyncio
async def test_knowledge_disabled_writes_nothing(monkeypatch):
    """停用：不寫檔、不注入——行為與舊版逐字相同。"""
    monkeypatch.setattr(config, "KNOWLEDGE_ENABLED", False)
    workspace.create_workspace("proj-k2")
    await _run_session("s3", "proj-k2", "重點: 不該被沉澱")
    assert "docs/RESEARCH.md" not in workspace.list_files("proj-k2")

    experts2 = await _run_session("s4", "proj-k2", "重點: 第二場")
    assert "既有調研" not in experts2["researcher"].prompts[0]


@pytest.mark.asyncio
async def test_pm_gets_prior_research_when_researcher_absent():
    """研究員缺席（離線/被關）時，過往調研沉澱仍注入 PM 拆解。"""
    workspace.create_workspace("proj-k3")
    await _run_session("s5", "proj-k3", "重點: 留給下一場的調研")

    async def broadcast(ev: events.StudioEvent) -> None:
        pass

    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: 實作", "決議: 完成", "檢討：無"]),
        "engineer": StubExpert(BY_KEY["engineer"], ["做好了"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }
    session = StudioSession(
        "s6",
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path("proj-k3"),
        workspace_id="proj-k3",
    )
    await session.run("繼續開發")
    assert "留給下一場的調研" in experts["pm"].prompts[0]
