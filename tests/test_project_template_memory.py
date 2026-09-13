"""跨專案模板記憶：從既有藍圖/ADR 萃取，注入同類型新專案藍圖生成。"""

from __future__ import annotations

import pytest

from studio import adr, blueprint, config, memory, projects, providers
from studio.improver import ProjectImprover


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", True)


def _seed_recipe_template(*, refresh: bool = True) -> dict:
    source = projects.create("家庭食譜工具", vision="整理料理食譜與採買清單")
    blueprint.save(
        source["id"],
        {
            "version": 1,
            "vision": "讓家庭能快速整理料理食譜",
            "users": "每天備餐的家庭使用者",
            "features": [
                {"title": "食譜分類", "priority": 0, "detail": "依料理類型整理"},
                {"title": "採買清單", "priority": 1, "detail": "從食譜產生缺料清單"},
            ],
            "milestones": [{"title": "M1 食譜可整理"}],
        },
        session_id="bp1",
    )
    adr.record(
        projects.workspace_dir(source["id"]),
        [{"decision": "本機優先用 SQLite 儲存食譜", "rationale": "資料量小且離線可用"}],
        session_id="adr1",
    )
    if refresh:
        assert memory.refresh_project_template(source["id"]) is True
    return source


def test_template_context_matches_similar_project_only():
    _seed_recipe_template()

    target = projects.create("食譜規劃器", vision="幫家庭規劃料理食譜")
    ctx = memory.template_context(target)
    assert "跨專案模板記憶" in ctx
    assert "家庭食譜工具" in ctx
    assert "食譜分類" in ctx and "採買清單" in ctx
    assert "SQLite" in ctx
    assert "任務:" not in ctx and "核心改動:" not in ctx

    unrelated = projects.create("發票掃描器", vision="把紙本發票轉成報表")
    assert memory.template_context(unrelated) == ""


def test_template_context_backfills_when_store_empty():
    _seed_recipe_template(refresh=False)
    target = projects.create("食譜助理", vision="安排料理食譜")

    ctx = memory.template_context(target)

    assert "跨專案模板記憶" in ctx
    assert "食譜分類" in ctx
    assert memory.template_path().is_file()


async def test_blueprint_prompt_injects_template_memory(monkeypatch):
    _seed_recipe_template()
    target = projects.create("食譜助理", vision="快速安排料理食譜")
    seen: list[str] = []

    class FakeExpert:
        async def speak(self, prompt, broadcast):
            seen.append(prompt)
            return "功能: [P0] 食譜分類"

        async def stop(self):
            pass

    monkeypatch.setattr(providers, "make_expert", lambda *args, **kwargs: FakeExpert())

    async def bc(ev):
        pass

    imp = ProjectImprover(target, bc)
    await imp._blueprint_with_pm(target["id"], "sid")

    assert seen and "跨專案模板記憶" in seen[0]
    assert "本機優先用 SQLite 儲存食譜" in seen[0]


def test_template_context_disabled_with_blueprint_flag(monkeypatch):
    _seed_recipe_template()
    target = projects.create("食譜規劃器", vision="料理食譜")
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", False)
    assert memory.template_context(target) == ""
