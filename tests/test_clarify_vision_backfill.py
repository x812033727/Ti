"""專案模式的願景回填與前綴：立項抽出的 `願景:` 寫回 meta（僅當為空）、下場前綴進需求。"""

from __future__ import annotations

import pytest

from studio import config, projects, ws


class _FakeSession:
    """只回傳固定 result 的假 session（ws._run_project_session 的最小介面）。"""

    def __init__(self, result: dict):
        self.session_id = "fake-sid"
        self._result = result
        self.requirement_received = ""

    async def run(self, requirement: str) -> dict:
        self.requirement_received = requirement
        return self._result


@pytest.fixture(autouse=True)
def _projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")


def test_update_vision_only_when_empty():
    meta = projects.create("記帳工具")
    assert meta["vision"] == ""
    updated = projects.update_vision(meta["id"], "最輕量的記帳工具")
    assert updated["vision"] == "最輕量的記帳工具"
    # 已有願景不覆寫
    again = projects.update_vision(meta["id"], "另一個願景")
    assert again["vision"] == "最輕量的記帳工具"
    # 空字串忽略、不存在專案安全回 None
    assert projects.update_vision(meta["id"], "  ")["vision"] == "最輕量的記帳工具"
    assert projects.update_vision("nosuch", "x") is None


@pytest.mark.asyncio
async def test_project_session_backfills_vision_and_prefixes():
    project = projects.create("記帳工具")
    session = _FakeSession(
        {"completed": True, "followups": [], "commit": None, "vision": "最輕量的記帳工具"}
    )
    await ws._run_project_session(session, "做記帳功能", project)
    # 建案時無願景 → 不前綴
    assert session.requirement_received == "做記帳功能"
    # 立項抽出的願景回填 meta
    assert projects.get(project["id"])["vision"] == "最輕量的記帳工具"

    # 下一場：既有願景前綴進需求
    project2 = projects.get(project["id"])
    session2 = _FakeSession({"completed": True, "followups": [], "commit": None, "vision": ""})
    await ws._run_project_session(session2, "加上分類統計", project2)
    assert "產品願景：最輕量的記帳工具" in session2.requirement_received
    assert "加上分類統計" in session2.requirement_received


@pytest.mark.asyncio
async def test_existing_vision_not_overwritten_by_session():
    project = projects.create("看板", vision="使用者自填的願景")
    session = _FakeSession(
        {"completed": True, "followups": [], "commit": None, "vision": "立項抽出的願景"}
    )
    await ws._run_project_session(session, "做看板", project)
    assert projects.get(project["id"])["vision"] == "使用者自填的願景"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
