"""端到端驗證：跨場次教訓庫在「真實 orchestrator 流程」中的接線。

不同於 tests/core/test_lessons.py（純函式 IO），本檔驅動真正的 StudioSession（注入 stub
專家、關閉 git、cwd=workspace），證明完整閉環：
  第 1 場檢討 PM 吐出 `教訓:` → 落盤 lessons.json → 第 2 場開場 PM 拆解 prompt 收到注入。

沿用 tests/test_qa_task3_notes.py 的 StubExpert 模式（不需 LLM / bwrap）。
"""

from __future__ import annotations

import pytest

from studio import config, events, lessons, workspace
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


def _collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


def _experts(pm, eng, qa, senior):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }


@pytest.fixture(autouse=True)
def _base_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(config, "LESSONS_MAX", 12)


async def _run_session(sid: str, pm_scripts: list[str]) -> dict:
    workspace.create_workspace(sid)
    _bucket, broadcast = _collect()
    experts = _experts(
        pm=pm_scripts,
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("做一個小工具")
    return experts


@pytest.mark.asyncio
async def test_loop_closes_lesson_persisted_then_injected(monkeypatch):
    """啟用後：第 1 場檢討的教訓落盤，第 2 場開場 PM 拆解收到注入。"""
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)

    # 第 1 場：PM 檢討（第 3 次發言）吐出一條教訓
    await _run_session(
        "s1",
        pm_scripts=[
            "任務: 實作核心功能",
            "決議: 完成",
            "檢討：整體順利。\n教訓: 浮點比較要用 math.isclose，別用 == 避免精度誤差",
        ],
    )

    # 落盤驗證
    stored = [r["text"] for r in lessons.all_lessons()]
    assert any("math.isclose" in s for s in stored), f"教訓未落盤：{stored}"

    # 第 2 場：開場 PM 拆解 prompt 應收到注入的教訓
    experts2 = await _run_session(
        "s2",
        pm_scripts=["任務: 實作另一功能", "決議: 完成", "檢討：無"],
    )
    decompose_prompt = experts2["pm"].prompts[0]
    assert "跨場次教訓庫" in decompose_prompt
    assert "math.isclose" in decompose_prompt


@pytest.mark.asyncio
async def test_loop_dormant_when_disabled(monkeypatch):
    """停用（預設）：檢討即使吐 `教訓:` 也不落盤，下場不注入——零行為變更。"""
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)

    experts1 = await _run_session(
        "d1",
        pm_scripts=["任務: 實作", "決議: 完成", "檢討。\n教訓: 這條不該被記住"],
    )
    # 停用時連檢討 prompt 都不該索取教訓
    retro_prompt = experts1["pm"].prompts[2]
    assert "教訓" not in retro_prompt
    assert lessons.all_lessons() == []

    experts2 = await _run_session("d2", pm_scripts=["任務: 實作", "決議: 完成", "檢討"])
    assert "跨場次教訓庫" not in experts2["pm"].prompts[0]


@pytest.mark.asyncio
async def test_appraisal_lessons_e2e_flow(monkeypatch):
    """考核教訓入庫 E2E：第 1 場低分考核落盤，第 2 場開場 PM 拆解 prompt 收到注入。"""
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)
    monkeypatch.setattr(config, "APPRAISAL_ENABLED", True)

    # 第 1 場：PM 檢討（第 3 次發言）吐出一條低分考核，沒有 retro 教訓
    await _run_session(
        "s1",
        pm_scripts=[
            "任務: 實作功能",
            "決議: 完成",
            "檢討：整體還行。\n考核: engineer 2 浮點數比較寫錯了，請用 math.isclose",
        ],
    )

    # 落盤驗證
    stored = [r["text"] for r in lessons.all_lessons()]
    assert any("考核教訓(2分): 浮點數比較寫錯了，請用 math.isclose" in s for s in stored), (
        f"考核教訓未落盤：{stored}"
    )

    # 第 2 場：開場 PM 拆解 prompt 應收到注入的考核教訓
    experts2 = await _run_session(
        "s2",
        pm_scripts=["任務: 實作另一功能", "決議: 完成", "檢討：無"],
    )
    decompose_prompt = experts2["pm"].prompts[0]
    assert "跨場次教訓庫" in decompose_prompt
    assert "考核教訓(2分): 浮點數比較寫錯了，請用 math.isclose" in decompose_prompt
