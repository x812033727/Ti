"""改善計畫成果物（_wrap_up 沉澱 docs/IMPROVEMENT.md）的單元測試。

驗證從檢討文字解析後續改善任務＋教訓、格式化成可累積的改善計畫；無項目時略過。純加性、
不影響完成判定（解析與 backlog 回填同一份、不重跑 LLM）。
"""

from __future__ import annotations

import pytest

from studio import config
from studio.orchestrator import StudioSession


async def _noop(ev):
    pass


def _session(monkeypatch):
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)
    s = StudioSession("t", _noop, cwd=None)
    s._requirement = "做一個登入頁"
    captured: dict[str, str] = {}
    s._persist_knowledge = lambda name, text: captured.__setitem__(name, text)
    return s, captured


def test_improvement_plan_formats_followups_and_lessons(monkeypatch):
    s, captured = _session(monkeypatch)
    retro = (
        "這次大致順利。\n"
        "後續任務: [P0/bug] 修登入逾時\n"
        "後續任務: 加端到端測試\n"
        "教訓: 用 fixture 隔離環境變數避免污染\n"
    )
    s._persist_improvement_plan(retro)
    assert "IMPROVEMENT.md" in captured
    md = captured["IMPROVEMENT.md"]
    assert "## 改善計畫 — 做一個登入頁" in md
    assert "### 後續改善任務" in md
    assert "[P0/bug] 修登入逾時" in md
    assert "[P1/improvement] 加端到端測試" in md  # 無標籤→預設 P1/improvement
    assert "### 可重用教訓" in md
    assert "用 fixture 隔離環境變數避免污染" in md


def test_improvement_plan_empty_skips(monkeypatch):
    s, captured = _session(monkeypatch)
    s._persist_improvement_plan("純文字檢討，沒有任何後續任務或教訓行。")
    assert captured == {}  # 無項目→不寫檔


def test_improvement_plan_lessons_only_when_enabled(monkeypatch):
    s, captured = _session(monkeypatch)
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    s._persist_improvement_plan("教訓: 這條在停用教訓時不該進改善計畫")
    assert captured == {}  # 只有教訓且教訓停用→無項目→略過


# --- 改善計畫讀回（閉合「驗證→改善計畫→行動」迴圈）---------------------------


class _RecExpert:
    """記錄收到 prompt 的 stub 專家（拆解只需 pm 真的發言）。"""

    def __init__(self, role):
        self.role = role
        self.prompts: list[str] = []

    async def speak(self, prompt, broadcast):
        self.prompts.append(prompt)
        return "任務: 做一件事"

    async def stop(self):
        pass


@pytest.mark.asyncio
async def test_improvement_plan_read_back_into_decompose(monkeypatch):
    import pytest as _pytest  # noqa: F401  (mark below)

    from studio.orchestrator import LaneContext
    from studio.roles import BY_KEY

    experts = {k: _RecExpert(BY_KEY[k]) for k in ("pm", "engineer", "qa", "senior")}
    s = StudioSession("t", _noop, experts=experts, cwd=None)
    s._main_ctx = LaneContext("main", None, experts, None)
    s._requirement = "做一個登入頁"
    # 過往場次沉澱的改善計畫（模擬 docs/IMPROVEMENT.md 讀回）。
    monkeypatch.setattr(
        s,
        "_knowledge_tail",
        lambda name: "後續改善任務：補登入逾時重試" if name == "IMPROVEMENT.md" else "",
    )

    # 隔離拆解的副作用（看板/commit 另有專測），聚焦在「改善計畫是否進 PM 拆解 prompt」。
    async def _noop_async(*a, **k):
        pass

    monkeypatch.setattr(s, "_board", _noop_async)
    monkeypatch.setattr(s, "_commit", _noop_async)

    await s._stage_decompose({"type": "decompose"})
    pm_prompt = experts["pm"].prompts[0]
    assert "docs/IMPROVEMENT.md" in pm_prompt
    assert "補登入逾時重試" in pm_prompt
