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

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
CONTRIB = ROOT / "CONTRIBUTING.md"
PYPROJECT = ROOT / "pyproject.toml"
SUBPROC_INV = ROOT / "studio" / "docs" / "subprocess_migration_inventory.md"
EPIC_BASE = "4f32d3a"  # 收斂 epic 起點前最後一個共同 commit
EPIC_END = "11e4a51"  # 收斂 epic 的完成快照（最後一個動到本 epic 測試/交付的 commit）
# 本守門驗證「CONTRIBUTING/README 收斂 epic 未引入新依賴、未弱化既有 docs 測試」。原以
# EPIC_BASE..HEAD 比對會隨 HEAD 前移而誤擋日後不相關的合法變更（如 issue #0001 的 uvicorn
# 升版）；改為固定 EPIC_BASE..EPIC_END，永久只驗證該 epic 自身的 diff，不再受後續工作干擾。


def _epic_range_in_clone() -> bool:
    """epic 起訖 commit 是否都在當前 clone（CI shallow fetch-depth:1 時不在 → 略過歷史比對）。"""
    return all(
        subprocess.run(
            ["git", "cat-file", "-e", f"{c}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
        for c in (EPIC_BASE, EPIC_END)
    )


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


# 標準6：pyproject 未被「收斂 epic」改動（固定 EPIC_BASE..EPIC_END，不隨後續工作變動）
def test_pyproject_unchanged():
    # shallow clone（CI fetch-depth:1）無歷史 commit → 略過（此為純歷史比對、與當前 HEAD 無關）。
    if not _epic_range_in_clone():
        pytest.skip(f"epic 範圍 {EPIC_BASE}..{EPIC_END} 不在 shallow clone，略過歷史 diff")
    r = subprocess.run(
        ["git", "diff", f"{EPIC_BASE}..{EPIC_END}", "--", str(PYPROJECT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and r.stdout.strip() == "", (
        f"收斂 epic 不應改動 pyproject:\n{r.stdout}"
    )


# 標準7：subprocess inventory 核心契約未被破壞（允許後續新增 metadata）
def test_subprocess_inventory_untouched():
    assert SUBPROC_INV.exists()
    text = SUBPROC_INV.read_text(encoding="utf-8")
    assert "定位採**函式錨點**" in text
    assert "`檔案.py::函式名`" in text
    assert "`tests/sandbox/test_qa_task1_subprocess_inventory.py` 以 AST 比對錨點" in text
    assert "| # | 檔案::錨點 | 內容 | 分類 | 理由 | 遷移注意 |" in text


# 標準5 收尾-d：本 epic 未偷改/放寬任何「既有」docs 測試（只可新增 QA 測試）
def test_no_preexisting_docs_test_weakened():
    if not _epic_range_in_clone():
        pytest.skip(
            f"epic 範圍 {EPIC_BASE}..{EPIC_END} 不在 shallow clone，無法做歷史 name-only diff"
        )
    r = subprocess.run(
        ["git", "diff", f"{EPIC_BASE}..{EPIC_END}", "--name-only", "--", "tests/docs/"],
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
