"""workspace.list_files 遍歷層剪枝後的行為等值守護。

契約（與剪枝前完全一致）：回傳相對路徑、排序穩定；_IGNORE 目錄整棵子樹不出現；
連「檔名本身」命中 _IGNORE 也排除；巢狀正常目錄照列。
"""

from __future__ import annotations

import pytest

from studio import config, workspace


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")


def test_list_files_prunes_ignored_and_sorts():
    root = workspace.create_workspace("s1")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x", encoding="utf-8")
    (root / "README.md").write_text("x", encoding="utf-8")
    # 雜訊子樹：整棵不出現（剪枝層直接不進入）
    deep = root / "node_modules" / "pkg" / "lib"
    deep.mkdir(parents=True)
    (deep / "index.js").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("x", encoding="utf-8")
    # 巢狀正常目錄內再藏一層雜訊目錄
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "main.cpython-311.pyc").write_text("x", encoding="utf-8")
    # 檔名本身命中 _IGNORE（極端 case，沿舊行為排除）
    (root / "src" / "node_modules").write_text("x", encoding="utf-8")

    assert workspace.list_files("s1") == ["README.md", "src/main.py"]


def test_list_files_missing_workspace_returns_empty():
    assert workspace.list_files("nosuch") == []
