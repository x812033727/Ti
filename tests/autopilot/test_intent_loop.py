"""意圖迴路(第 4 階 B3):專案常駐 intent + improver 找問題差距分析注入。

守護不變量:
- projects.set_intent:可覆寫、空=清除、夾 2000、專案不存在回 None;不觸發任何執行。
- improver._intent_context:TI_INTENT_LOOP=0(預設)或無 intent → 空字串(零行為變更);
  有 intent → 帶意圖原文+差距分析指令;每次現讀 meta(intent 更新下一輪即生效)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from studio import config, improver, projects


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws", raising=False)
    return tmp_path


def _mkproject():
    meta = projects.create("測試產品", vision="讓小店能線上收款")
    assert meta is not None
    return meta


def test_set_intent_overwrite_clear_and_missing(_state):
    meta = _mkproject()
    pid = meta["id"]
    out = projects.set_intent(pid, "把結帳流程做到可正式收費")
    assert out["intent"] == "把結帳流程做到可正式收費"
    out = projects.set_intent(pid, "改攻訂閱制")  # 可覆寫(與 update_vision 只補空不同)
    assert out["intent"] == "改攻訂閱制"
    assert projects.get(pid)["intent"] == "改攻訂閱制", "落盤持久化"
    out = projects.set_intent(pid, "")
    assert out["intent"] == ""
    assert projects.set_intent("nope", "x") is None
    long = "x" * 5000
    assert len(projects.set_intent(pid, long)["intent"]) == 2000, "夾長度"


def test_intent_context_gating_and_freshness(_state, monkeypatch):
    meta = _mkproject()
    pid = meta["id"]
    projects.set_intent(pid, "把結帳流程做到可正式收費")
    stub = SimpleNamespace(project={"id": pid})

    monkeypatch.setattr(config, "INTENT_LOOP", False)
    assert improver.ProjectImprover._intent_context(stub) == "", "旗標關=零注入"

    monkeypatch.setattr(config, "INTENT_LOOP", True)
    ctx = improver.ProjectImprover._intent_context(stub)
    assert "把結帳流程做到可正式收費" in ctx and "差距分析" in ctx

    projects.set_intent(pid, "改攻訂閱制")
    assert "改攻訂閱制" in improver.ProjectImprover._intent_context(stub), "現讀 meta,更新即生效"

    projects.set_intent(pid, "")
    assert improver.ProjectImprover._intent_context(stub) == "", "清除後零注入"
