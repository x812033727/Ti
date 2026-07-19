"""專家技能唯讀彙整(Kimi 化 PR8):供面板「插件」頁列出內部技能。

單一真相:名稱白名單=experts.EXPERT_SKILLS_LIST,內容=repo .claude/skills/<name>/SKILL.md
的 frontmatter(name/description)。純檔案 IO、無 LLM;讀不到的技能回列名+空描述,
絕不拋——觀測面不得因一個壞檔死掉。
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config
from .experts import EXPERT_SKILLS_LIST

_DESC_RE = re.compile(r"^description:\s*(.+)$", re.M)


def _skill_dir() -> Path:
    return Path(config.PROJECT_ROOT) / ".claude" / "skills"


def list_skills() -> dict:
    """回 {"enabled": bool, "roles": [...], "skills": [{name, description}]}。"""
    skills = []
    for name in EXPERT_SKILLS_LIST:
        desc = ""
        try:
            text = (_skill_dir() / name / "SKILL.md").read_text(encoding="utf-8")
            m = _DESC_RE.search(text.split("---", 2)[1] if text.startswith("---") else text)
            if m:
                desc = m.group(1).strip()
        except (OSError, IndexError):
            pass
        skills.append({"name": name, "description": desc})
    return {
        "enabled": bool(config.EXPERT_SKILLS),
        "roles": sorted(config.EXPERT_SKILLS_ROLES),
        "skills": skills,
    }
