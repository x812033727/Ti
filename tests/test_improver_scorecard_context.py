"""記分卡回饋進改良迴圈（roadmap 階段三）：_scorecard_context 摘要與注入點守護。

契約：有歷史 scorecard → 產出繁中量化摘要並注入「找問題」prompt 與 _compose_requirement；
無資料/取不到 meta/無 scorecard/記憶額度 0 → 回空字串、prompt 不帶標頭、絕不報錯；
摘要不得含 `任務:`/`核心改動:` 等 marker 字樣（防污染 flow 解析）；
routes._aggregate_scorecard 為 history.aggregate_scorecard 的 alias（平移守護）。
"""

from __future__ import annotations

import pytest

from studio import config, history, projects, routes
from studio.improver import ProjectImprover


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 20)


def _improver():
    project = projects.create("測試產品", vision="最好用的小工具")

    async def bc(ev):
        pass

    return ProjectImprover(project, bc), project


def _fake_meta(sid: str, status: str, scorecard: dict | None) -> dict:
    meta = {"session_id": sid, "started_at": 100.0, "status": status}
    if scorecard is not None:
        meta["scorecard"] = scorecard
    return meta


_METAS = {
    "s1": _fake_meta(
        "s1",
        "completed",
        {"avg_rounds": 2.0, "qa_total": 2, "qa_pass": 2, "rejects": {"qa_fail": 2}},
    ),
    "s2": _fake_meta(
        "s2",
        "completed",
        {"avg_rounds": 3.0, "qa_total": 1, "qa_pass": 1, "rejects": {"smoke_fail": 1}},
    ),
    "s3": _fake_meta("s3", "incomplete", {"qa_total": 1, "qa_pass": 0, "rejects": {}}),
}


def _seed_sessions(project: dict, monkeypatch, metas: dict) -> None:
    for sid in metas:
        projects.record_session(project["id"], sid, f"task-{sid}", True)
    monkeypatch.setattr(history, "get_meta", lambda sid: metas.get(sid))


def test_summary_reflects_aggregate(monkeypatch):
    imp, project = _improver()
    _seed_sessions(project, monkeypatch, _METAS)

    ctx = imp._scorecard_context()
    assert "【本專案近 3 場量化成績單】" in ctx
    assert "完成率 67%" in ctx  # 2/3 completed
    assert "QA 通過率 75%" in ctx  # (2+1+0)/(2+1+1)
    assert "平均輪數 2.5" in ctx  # mean(2.0, 3.0)
    assert "QA 驗證失敗 2 次" in ctx and "自測失敗 1 次" in ctx
    # marker 中性：不得出現會被 flow 解析的結構化行首字樣
    assert "任務:" not in ctx and "核心改動:" not in ctx


def test_injected_into_discover_and_requirement(monkeypatch):
    imp, project = _improver()
    _seed_sessions(project, monkeypatch, _METAS)

    prompts = imp._discover_prompts(project["id"])
    for key in ("senior", "pm", "researcher"):
        assert "量化成績單" in prompts[key]

    req = imp._compose_requirement({"title": "改良任務", "detail": ""})
    assert "量化成績單" in req
    assert "本輪改良任務：改良任務" in req  # 原有結構不受影響


@pytest.mark.parametrize(
    "case",
    ["no_sessions", "meta_none", "no_scorecard", "memory_zero"],
)
def test_black_samples_return_empty(monkeypatch, case):
    imp, project = _improver()
    if case == "no_sessions":
        monkeypatch.setattr(history, "get_meta", lambda sid: _METAS.get(sid))
    elif case == "meta_none":
        _seed_sessions(project, monkeypatch, _METAS)
        monkeypatch.setattr(history, "get_meta", lambda sid: None)  # 已被 retention 回收
    elif case == "no_scorecard":
        metas = {sid: _fake_meta(sid, "completed", None) for sid in ("s1", "s2")}
        _seed_sessions(project, monkeypatch, metas)
    elif case == "memory_zero":
        _seed_sessions(project, monkeypatch, _METAS)
        monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 0)

    assert imp._scorecard_context() == ""
    assert "量化成績單" not in imp._discover_prompts(project["id"])["senior"]
    assert "量化成績單" not in imp._compose_requirement({"title": "t"})


def test_failure_never_breaks_loop(monkeypatch):
    """回饋只是優化：取 meta 途中丟例外 → 回空字串，不炸改良迴圈。"""
    imp, project = _improver()
    _seed_sessions(project, monkeypatch, _METAS)

    def boom(sid):
        raise RuntimeError("disk error")

    monkeypatch.setattr(history, "get_meta", boom)
    assert imp._scorecard_context() == ""


def test_routes_alias_preserved():
    """聚合平移守護：routes 舊名必須仍指向 history 的同一函式（既有呼叫端/測試不破）。"""
    assert routes._aggregate_scorecard is history.aggregate_scorecard
