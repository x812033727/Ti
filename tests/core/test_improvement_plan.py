"""改善計畫成果物（_wrap_up 沉澱 docs/IMPROVEMENT.md）的單元測試。

驗證從檢討文字解析後續改善任務＋教訓、格式化成可累積的改善計畫；無項目時略過。純加性、
不影響完成判定（解析與 backlog 回填同一份、不重跑 LLM）。
"""

from __future__ import annotations

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
