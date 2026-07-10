"""Claude 專家 skills 漸進揭露(效率強化 C,預設關灰度)。

守護不變量:
- 旋鈕開+白名單角色 → options 帶 skills=EXPERT_SKILLS_LIST 且 setting_sources=["project"]
  (顯式隔離——SDK 一設 skills 會自動改成 ["user","project"],會誤吃主機 user 層技能)。
- 非白名單角色(pm)/旋鈕關 → 兩者皆缺省。
- SKILL.md 靜態守門:檔案存在、frontmatter name=目錄名(防改名後列名白名單無聲斷鏈)、
  白名單與磁碟目錄一致(單一真相)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from studio import config, experts
from studio.roles import ENGINEER, PM

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"


@pytest.fixture
def skills_on(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_SKILLS", True)
    monkeypatch.setattr(config, "EXPERT_SKILLS_ROLES", frozenset({"engineer", "senior", "qa"}))


def test_skills_options_for_whitelisted_role(skills_on):
    opts = experts._skills_options(ENGINEER)
    assert opts["skills"] == list(experts.EXPERT_SKILLS_LIST)
    assert opts["setting_sources"] == ["project"], (
        "必須顯式鎖 project 層——SDK 預設會納入主機 user 層技能"
    )


def test_skills_options_skips_non_whitelisted_and_knob_off(skills_on, monkeypatch):
    assert experts._skills_options(PM) == {}, "PM 不寫碼,不給 skills"
    monkeypatch.setattr(config, "EXPERT_SKILLS", False)
    assert experts._skills_options(ENGINEER) == {}, "旋鈕關(預設)完全缺省"


def test_skill_files_exist_and_names_match_dirs():
    for name in experts.EXPERT_SKILLS_LIST:
        skill_md = SKILLS_DIR / name / "SKILL.md"
        assert skill_md.is_file(), f"白名單技能 {name} 缺 SKILL.md(列名白名單無聲斷鏈)"
        text = skill_md.read_text(encoding="utf-8")
        m = re.search(r"^name:\s*(\S+)", text, re.M)
        assert m and m.group(1) == name, f"{name}/SKILL.md 的 frontmatter name 必須等於目錄名"
        assert re.search(r"^description:\s*\S", text, re.M), f"{name} 缺 description(漸進揭露靠它)"


def test_skill_dirs_have_no_orphans():
    on_disk = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    assert on_disk == set(experts.EXPERT_SKILLS_LIST), (
        f"磁碟技能目錄與白名單不一致:disk={on_disk} whitelist={set(experts.EXPERT_SKILLS_LIST)}"
        "——新增技能要同步進 EXPERT_SKILLS_LIST,否則靜默失效"
    )
