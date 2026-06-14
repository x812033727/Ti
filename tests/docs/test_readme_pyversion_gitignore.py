"""任務 #3 驗收（驗收標準 4）：
- README 標明需 Python ≥3.11，且與 pyproject `requires-python` 一致。
- README 明確提到 `.venv/` 已在 `.gitignore`、不提交；且 .gitignore 實際含 .venv/。
- 文件聲明與「執行環境前置」段對齊，並對齊當前執行環境（≥3.11）。
"""

import re
import sys

from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = (ROOT / "README.md").read_text(encoding="utf-8")
GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _env_section() -> str:
    m = re.search(r"^##\s+執行環境前置\s*$(.*?)(?=^##\s|\Z)", README, re.MULTILINE | re.DOTALL)
    assert m, "找不到『執行環境前置』段落"
    return m.group(1)


def test_pyproject_requires_310():
    assert 'requires-python = ">=3.11"' in PYPROJECT


def test_readme_states_python_310_in_env_section():
    """前置段須標明 Python ≥3.11（容許 ≥/>=/3.11+ 等寫法）。"""
    sec = _env_section()
    assert re.search(r"Python\s*(≥|>=)?\s*3\.11|3\.11\s*\+", sec), "前置段未明確標明 Python 3.11"


def test_readme_version_consistent_with_pyproject():
    """README 不得出現與 >=3.11 矛盾的最低版本要求（如 3.8/3.9）。"""
    bad = re.findall(r"Python\s*(?:≥|>=)?\s*3\.(?:[6-9])\b", README)
    assert not bad, f"README 出現與 pyproject 矛盾的版本要求: {bad}"


def test_readme_mentions_gitignore_for_venv():
    """前置段明確提到 .venv/ 在 .gitignore、不進版控。"""
    sec = _env_section()
    assert ".gitignore" in sec, "前置段未提及 .gitignore"
    assert re.search(r"不(會)?進版控|不提交", sec), "前置段未說明不進版控/不提交"


def test_gitignore_actually_ignores_venv():
    """.gitignore 實際含 .venv/ 條目（文件聲明屬實）。"""
    lines = [ln.strip() for ln in GITIGNORE.splitlines()]
    assert ".venv/" in lines, ".gitignore 未實際忽略 .venv/"


def test_runtime_python_satisfies_requirement():
    """當前驗證環境本身 ≥3.11，確保文件要求對應可達成的真實環境。"""
    assert sys.version_info >= (3, 11), f"執行環境 Python 過舊: {sys.version_info}"
