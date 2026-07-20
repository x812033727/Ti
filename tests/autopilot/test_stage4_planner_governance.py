"""Stage 4 規畫器只能消費版本化 intent、真實指標、事故與有效 backlog。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from studio import autonomy, autopilot, backlog, config, experts, improver, projects


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspace", raising=False)
    monkeypatch.setattr(config, "AUTOPILOT_NORTH_STAR", "不得進入 Stage 4 prompt 的舊目標")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def _stage4(project_id: str) -> dict:
    autonomy.ensure_policy(project_id)
    return autonomy.save_policy(
        project_id,
        {
            "stage": 4,
            "intent": {
                "north_star": "讓健康部署率達 99.9%",
                "success_metrics": ["healthy_deploy_rate>=0.999"],
                "forbidden_actions": ["不得刪除正式資料"],
            },
        },
    )


class _FakeExpert:
    prompts: list[str] = []

    def __init__(self, role, sid, cwd):
        pass

    async def speak(self, prompt, broadcast):
        self.prompts.append(prompt)
        return "任務: 修復健康檢查失敗後未回滾的路徑"

    async def stop(self):
        return None


@pytest.mark.asyncio
async def test_core_discovery_uses_only_stage4_evidence(monkeypatch):
    _stage4(autonomy.CORE_PROJECT_ID)
    backlog.add("修復現有部署告警", source="manual", risk="medium", eligible=True)
    autonomy.emit_event(
        "autonomy_decision",
        project_id=autonomy.CORE_PROJECT_ID,
        run_id="failed-run",
        outcome="run_started",
        eligible=True,
    )
    autonomy.emit_event(
        "autonomy_decision",
        project_id=autonomy.CORE_PROJECT_ID,
        run_id="failed-run",
        outcome="deploy_failed",
        eligible=True,
        cost_usd=1.5,
    )
    _FakeExpert.prompts = []
    monkeypatch.setattr(experts, "Expert", _FakeExpert)

    assert await autopilot._evaluate_self(str(config.AUTOPILOT_STATE_DIR)) == 1
    prompt = _FakeExpert.prompts[0]
    assert "Stage 4 版本化規畫證據" in prompt
    assert "讓健康部署率達 99.9%" in prompt
    assert "healthy_deploy_rate>=0.999" in prompt
    assert "deploy_failed" in prompt
    assert "修復現有部署告警" in prompt
    assert "不得進入 Stage 4 prompt 的舊目標" not in prompt


def test_project_improver_uses_policy_even_when_legacy_flag_is_off(monkeypatch):
    meta = projects.create("Stage 4 專案", vision="舊願景")
    _stage4(meta["id"])
    backlog.add("有效既有工作", state_dir=projects.state_dir(meta["id"]), eligible=True)
    monkeypatch.setattr(config, "INTENT_LOOP", False)
    stub = SimpleNamespace(project={"id": meta["id"]})

    context = improver.ProjectImprover._intent_context(stub)
    assert "Stage 4 版本化規畫證據" in context
    assert "有效既有工作" in context
    assert "不得刪除正式資料" in context
    source_stub = SimpleNamespace(_intent_context=lambda: context)
    assert improver.ProjectImprover._discovery_source(source_stub) == "intent"


def test_planner_evidence_excludes_ineligible_and_terminal_backlog():
    meta = projects.create("證據專案")
    _stage4(meta["id"])
    sdir = projects.state_dir(meta["id"])
    kept = backlog.add("有效排隊工作", state_dir=sdir, eligible=True)
    excluded = backlog.add(
        "人工專屬工作",
        state_dir=sdir,
        eligible=False,
        exclusion_reason="人工專屬",
    )
    done = backlog.add("已完成工作", state_dir=sdir, eligible=True)
    backlog.set_status(done["id"], "done", state_dir=sdir)

    evidence = autonomy.planner_evidence(meta["id"])
    titles = [item["title"] for item in evidence["active_backlog"]]
    assert kept["title"] in titles
    assert excluded["title"] not in titles
    assert done["title"] not in titles
