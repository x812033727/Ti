"""QA 驗收測試 — 任務 #4：docs 測試全綠 + 更新 CONTRIBUTING PR checklist。

驗收重點（標準 5、6、7 與收尾）：
- CONTRIBUTING「PR 前的檢查清單」已更新：ruff/pytest 指令皆 .venv 前綴，
  並新增「dev 指令只在本文件維護、其他文件不得重複可複製區塊」的防漂移項；
- 收斂未引入新工具/新依賴（無 Makefile / cog / embedmd，pyproject 未動）；
- subprocess inventory 未被更動。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from _repo import REPO_ROOT

ROOT = REPO_ROOT
CONTRIB = ROOT / "CONTRIBUTING.md"
PYPROJECT = ROOT / "pyproject.toml"
SUBPROC_INV = ROOT / "studio" / "docs" / "subprocess_migration_inventory.md"


def _contrib() -> str:
    return CONTRIB.read_text(encoding="utf-8")


def _checklist_block() -> str:
    """擷取「PR 前的檢查清單」起到下一個 `## ` 標題之間。"""
    ls = _contrib().splitlines()
    start = next((i for i, ln in enumerate(ls) if "PR 前的檢查清單" in ln), None)
    assert start is not None, "CONTRIBUTING 找不到『PR 前的檢查清單』"
    end = next((i for i in range(start + 1, len(ls)) if ls[i].startswith("## ")), len(ls))
    return "\n".join(ls[start:end])


# 標準4 收尾-a：checklist 的 ruff 指令已補 .venv 前綴
def test_checklist_ruff_prefixed():
    blk = _checklist_block()
    assert ".venv/bin/python -m ruff check ." in blk, "checklist 的 ruff check 未補 .venv 前綴"
    assert ".venv/bin/python -m ruff format --check ." in blk, (
        "checklist 的 ruff format 未補 .venv 前綴"
    )
    # 不應殘留裸 ruff 指令（行首或 `ruff check` 開頭的 checklist 項）
    bad = [ln for ln in blk.splitlines() if re.search(r"\]\s*`?ruff (check|format)", ln)]
    assert not bad, f"checklist 仍有裸 ruff 指令: {bad}"


# 標準4 收尾-b：checklist 保留 .venv/bin/python -m pytest -q 全綠項
def test_checklist_pytest_present():
    assert ".venv/bin/python -m pytest -q" in _checklist_block(), "checklist 缺 pytest -q 全綠項"


# 標準3/5 收尾-c：checklist 新增防漂移項（dev 指令唯一維護、其他文件不得重複）
def test_checklist_has_antidrift_item():
    blk = _checklist_block()
    assert re.search(r"dev 指令.*(只在本文件|唯一).*維護", blk) or "重複" in blk, (
        "checklist 未新增『dev 指令只在本文件維護、不得重複』的防漂移項"
    )
    # 防漂移項應點名 README/其他文件僅以敘述或連結引用
    assert "README" in blk and ("連結" in blk or "敘述" in blk), (
        "防漂移項未說明 README 等文件僅以敘述或連結引用"
    )


# 標準2 不退步：pytest -q 仍 >=2 處（canonical 區塊 + checklist）
def test_pytest_q_count_still_ge2():
    occ = re.findall(r"\.venv/bin/python -m pytest -q", _contrib())
    assert len(occ) >= 2, f"CONTRIBUTING pytest -q 應 >=2 處，實得 {len(occ)}"


# 標準6：未引入 Makefile / cog / embedmd
def test_no_new_tooling_introduced():
    assert not (ROOT / "Makefile").exists(), "不應新增 Makefile"
    haystack = ""
    for p in (PYPROJECT, ROOT / ".pre-commit-config.yaml"):
        if p.exists():
            haystack += p.read_text(encoding="utf-8").lower()
    for tok in ("cog", "embedmd", "code-embedder"):
        assert tok not in haystack, f"不應引入嵌入工具 {tok!r}"


# 標準6：pyproject 自 epic 起點未被改動
def test_pyproject_unchanged():
    r = subprocess.run(
        ["git", "diff", "4f32d3a..HEAD", "--", str(PYPROJECT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and r.stdout.strip() == "", f"pyproject 不應被改動:\n{r.stdout}"


# 標準7：subprocess inventory 未被更動
def test_subprocess_inventory_untouched():
    assert SUBPROC_INV.exists()
    r = subprocess.run(
        ["git", "status", "--short", str(SUBPROC_INV)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip() == "", f"inventory 不應被更動: {r.stdout!r}"


# 標準5 收尾-d：本 epic 未偷改/放寬任何「既有」docs 測試（只可新增 QA 測試）
def test_no_preexisting_docs_test_weakened():
    r = subprocess.run(
        ["git", "diff", "4f32d3a..HEAD", "--name-only", "--", "tests/docs/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    changed = [Path(x).name for x in r.stdout.split() if x.strip()]
    # 允許清單：本次 QA 新增的測試檔
    allowed = {
        "test_qa_task1_dedup_inventory.py",
        "test_qa_task2_contributing_canonical.py",
        "test_qa_task3_readme_test_section.py",
        "test_qa_task4_pr_checklist.py",
    }
    illegal = [f for f in changed if f not in allowed]
    assert not illegal, f"既有 docs 測試被改動（疑似放寬斷言，須說明）: {illegal}"
