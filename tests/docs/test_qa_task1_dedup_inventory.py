"""QA 驗收測試 — 任務 #1：盤點 README ↔ CONTRIBUTING dev 指令重複。

任務 #1 的交付物是盤點文件 `studio/docs/dev_command_dedup_inventory.md`，
其職責為：標出 (a) 重複的指令區塊、(b) canonical 來源、(c) 所有引用點、
(d) 會受影響的 docs 測試斷言。

本測試「驗證盤點本身的正確性」——即盤點所宣稱的事實必須與 repo 現況一致，
而非驗證後續 #2/#3/#4 的收斂結果（那時 README 仍保留重複區塊，屬正常）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from _repo import REPO_ROOT

ROOT = REPO_ROOT
INVENTORY = ROOT / "studio" / "docs" / "dev_command_dedup_inventory.md"
README = ROOT / "README.md"
CONTRIB = ROOT / "CONTRIBUTING.md"
SUBPROC_INV = ROOT / "studio" / "docs" / "subprocess_migration_inventory.md"

# 盤點所宣稱「會受影響」的 docs 測試檔，皆須真實存在
REFERENCED_TEST_FILES = [
    "test_docs_pytest_command.py",
    "test_readme_consistency.py",
    "test_qa_task3_precommit_step.py",
    "test_readme_verify_cmd.py",
    "test_qa_task6_docs.py",
]

# 盤點所列「受影響斷言」中明確點名的測試函式（須真的定義於對應檔）
REFERENCED_TEST_FUNCS = [
    "test_readme_no_bare_pytest_command",
    "test_contributing_pytest_prefix",
    "test_contributing_venv_python3",
    "test_all_pytest_run_commands_prefixed",
    "test_windows_cross_platform_noted",
    "test_inventory_untouched",
    "test_venv_python_exists_and_runs",
]


def _txt(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# A. 交付物存在且非空
def test_inventory_doc_exists():
    assert INVENTORY.exists(), "task #1 盤點文件不存在"
    assert len(_txt(INVENTORY).strip()) > 200, "盤點文件內容過於單薄"


# B. 標出 canonical 來源 = CONTRIBUTING.md
def test_inventory_declares_canonical_contributing():
    t = _txt(INVENTORY)
    assert "canonical" in t.lower(), "盤點未標示 canonical 概念"
    # canonical 必須指向 CONTRIBUTING.md
    assert re.search(r"canonical[^\n]*CONTRIBUTING\.md|CONTRIBUTING\.md[^\n]*canonical", t, re.I), (
        "盤點未明確把 CONTRIBUTING.md 標為 canonical"
    )


# C. 盤點列出全部 4 組重複指令家族
def test_inventory_lists_all_duplicate_commands():
    t = _txt(INVENTORY)
    families = {
        "pip install -e": 'pip install -e ".[dev]"',
        "pytest": "pytest",
        "ruff": "ruff",
        "pre-commit": "pre-commit",
    }
    missing = [name for name, token in families.items() if token not in t]
    assert not missing, f"盤點漏列重複指令家族: {missing}"


# D. 盤點所宣稱的「重複」必須是真的：README 測試段與 CONTRIBUTING 皆含這些指令
def test_duplication_is_real_in_both_files():
    readme = _txt(README)
    contrib = _txt(CONTRIB)
    # README `## 測試` 段目前確實保留可複製執行區塊（#3 尚未收斂，屬正常）
    assert re.search(r"^## 測試", readme, re.M), "README 找不到 `## 測試` 段"
    for token in ('pip install -e ".[dev]"', "-m pytest", "ruff", "pre_commit"):
        assert token in readme, f"README 測試段應仍含重複指令 {token!r}（盤點宣稱的引用點）"
    for token in ('pip install -e ".[dev]"', "pytest", "ruff", "pre-commit"):
        assert token in contrib, f"CONTRIBUTING（canonical）應含 {token!r}"


# E. 盤點宣稱的 README 引用點（## 測試 段 code block）真實存在
def test_readme_reference_point_block_exists():
    ls = _txt(README).splitlines()
    # 找 `## 測試` 後第一個 ```bash fenced block，且含 pytest 指令
    sec = next((i for i, ln in enumerate(ls) if re.match(r"^## 測試", ln)), None)
    assert sec is not None, "README 無 `## 測試` 段"
    fence = next((i for i in range(sec, len(ls)) if ls[i].strip().startswith("```bash")), None)
    assert fence is not None, "README `## 測試` 段缺 ```bash 可複製執行區塊（盤點引用點）"
    end = next((i for i in range(fence + 1, len(ls)) if ls[i].strip() == "```"), None)
    assert end is not None, "README 測試段 code block 未正確閉合"
    block = "\n".join(ls[fence : end + 1])
    assert "-m pytest" in block, "README 測試段 code block 應含 pytest 指令"


# F. 盤點點名的「受影響 docs 測試檔」皆真實存在
def test_referenced_test_files_exist():
    docs_dir = ROOT / "tests" / "docs"
    missing = [f for f in REFERENCED_TEST_FILES if not (docs_dir / f).exists()]
    assert not missing, f"盤點引用了不存在的測試檔: {missing}"


# G. 盤點點名的測試函式皆真實定義（避免列了不存在的斷言）
def test_referenced_test_functions_exist():
    blob = "\n".join(
        (ROOT / "tests" / "docs" / f).read_text(encoding="utf-8")
        for f in REFERENCED_TEST_FILES
        if (ROOT / "tests" / "docs" / f).exists()
    )
    missing = [fn for fn in REFERENCED_TEST_FUNCS if f"def {fn}(" not in blob]
    assert not missing, f"盤點引用了不存在的測試函式: {missing}"


# H. 盤點作業未更動 subprocess inventory（標準 7 同源約束）
def test_subprocess_inventory_untouched():
    assert SUBPROC_INV.exists(), "subprocess_migration_inventory.md 不應消失"
    r = subprocess.run(
        ["git", "status", "--short", str(SUBPROC_INV)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip() == "", f"subprocess inventory 不應被更動: {r.stdout!r}"
