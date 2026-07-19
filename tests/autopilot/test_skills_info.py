"""技能唯讀彙整(Kimi 化 PR8):白名單=experts.EXPERT_SKILLS_LIST 單一真相、壞檔容錯。"""

from __future__ import annotations

from studio import config, skills_info
from studio.experts import EXPERT_SKILLS_LIST


def test_list_skills_shape_and_descriptions():
    out = skills_info.list_skills()
    assert set(out) == {"enabled", "roles", "skills"}
    assert [s["name"] for s in out["skills"]] == list(EXPERT_SKILLS_LIST), "白名單順序單一真相"
    for s in out["skills"]:
        assert s["description"], f"repo 內建技能應讀得到描述:{s['name']}"
    assert isinstance(out["enabled"], bool) and isinstance(out["roles"], list)


def test_list_skills_tolerates_missing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(skills_info, "_skill_dir", lambda: tmp_path / "nope")
    out = skills_info.list_skills()
    assert [s["description"] for s in out["skills"]] == [""] * len(
        EXPERT_SKILLS_LIST
    ), "讀不到=空描述,絕不拋"


def test_roles_reflect_config(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_SKILLS", True)
    monkeypatch.setattr(config, "EXPERT_SKILLS_ROLES", frozenset({"qa", "engineer"}))
    out = skills_info.list_skills()
    assert out["enabled"] is True and out["roles"] == ["engineer", "qa"]
