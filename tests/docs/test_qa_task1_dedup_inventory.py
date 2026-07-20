"""QA 驗收測試 — 任務 #1：盤點 README ↔ CONTRIBUTING dev 指令重複。

任務 #1 的交付物是盤點文件 `studio/docs/dev_command_dedup_inventory.md`，
其職責為：標出 (a) 重複的指令區塊、(b) canonical 來源、(c) 所有引用點、
(d) 會受影響的 docs 測試斷言。

本測試「驗證盤點本身的正確性」——即盤點所宣稱的事實必須與 repo 現況一致，
而非驗證後續 #2/#3/#4 的收斂結果（那時 README 仍保留重複區塊，屬正常）。
"""

from __future__ import annotations

import re
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


# D. 盤點所宣稱的「重複」記載必須正確：盤點文件須把 README 記為引用點、
#    CONTRIBUTING 記為 canonical；且 canonical 端（穩定事實）確實含這些指令家族。
#    注意：不對 README 活現況斷言「仍重複」——README 會被 #3 收斂，那是 epic 目標，
#    對暫態固化會在 #3 後誤判 fail（時序炸彈）。盤點正確性改讀盤點文件的記載。
def test_duplication_recorded_in_inventory():
    inv = _txt(INVENTORY)
    contrib = _txt(CONTRIB)
    # 盤點須記載 README 為引用點、CONTRIBUTING 為 canonical（敘述記載，與 README 是否已收斂無關）
    assert "README" in inv and "CONTRIBUTING" in inv, "盤點未同時記載 README 與 CONTRIBUTING"
    assert re.search(r"README[^\n]*(引用|測試)", inv), "盤點未把 README 記載為引用點"
    # canonical 端為穩定事實，task#2 後仍須含這些指令家族
    for token in ('pip install -e ".[dev]"', "pytest", "ruff", "pre-commit"):
        assert token in contrib, f"CONTRIBUTING（canonical）應含 {token!r}"


# E. 盤點明確記載 README 的引用點位置（## 測試 段）。
#    只驗「盤點是否記載此引用點」與「README 該段標題存在」（#3 收斂後仍保留標題，
#    僅把區塊改寫為摘要＋連結）——不要求 README 測試段保留 ```bash code block，
#    否則 #3 收斂後必 fail。
def test_inventory_records_readme_reference_point():
    inv = _txt(INVENTORY)
    # 盤點須點名 README `## 測試` 段為引用點
    assert "## 測試" in inv, "盤點未記載 README `## 測試` 引用點"
    # README 該段標題本身是穩定錨點（#3 只改寫內容，不刪標題）
    assert re.search(r"^## 測試", _txt(README), re.M), "README 找不到 `## 測試` 段標題"


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


# H. subprocess inventory 的核心遷移契約仍在（允許後續新增 metadata）
def test_subprocess_inventory_untouched():
    assert SUBPROC_INV.exists(), "subprocess_migration_inventory.md 不應消失"
    text = _txt(SUBPROC_INV)
    assert "定位採**函式錨點**" in text
    assert "`檔案.py::函式名`" in text
    assert "`tests/sandbox/test_qa_task1_subprocess_inventory.py` 以 AST 比對錨點" in text
    assert "| # | 檔案::錨點 | 內容 | 分類 | 理由 | 遷移注意 |" in text
