"""QA 獨立驗證（任務 #12 / 驗收標準 3）：safe_resolve 對合法輸入回正確 Path、內部 symlink 放行。

聚焦『回傳值正確性』：不只是 not None，而是精確等於解析後的絕對路徑，且：
- 回傳為絕對且 is_relative_to(root)；
- 內部 symlink（含指向子目錄、目錄 symlink）解析到真實目標並放行；
- 同一路徑的不同寫法（含冗餘 ./、單層 . 段）正規化到同一結果。
"""

from __future__ import annotations

import pytest

from studio import workspace


@pytest.fixture
def root(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# --- 合法檔案：回正確解析後 Path ---


def test_top_level_file_exact_path(root):
    (root / "a.txt").write_text("x", encoding="utf-8")
    result = workspace.safe_resolve(root, "a.txt")
    assert result == (root / "a.txt").resolve()
    assert result.is_absolute()
    assert result.is_relative_to(root.resolve())


def test_nested_file_exact_path(root):
    (root / "pkg" / "sub").mkdir(parents=True)
    f = root / "pkg" / "sub" / "m.py"
    f.write_text("y", encoding="utf-8")
    assert workspace.safe_resolve(root, "pkg/sub/m.py") == f.resolve()


def test_directory_target_returns_its_resolved_path(root):
    (root / "d").mkdir()
    assert workspace.safe_resolve(root, "d") == (root / "d").resolve()


def test_target_equals_root(root):
    assert workspace.safe_resolve(root, "") == root.resolve()


def test_redundant_dot_segments_normalized(root):
    (root / "a.txt").write_text("x", encoding="utf-8")
    assert workspace.safe_resolve(root, "./a.txt") == (root / "a.txt").resolve()
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("y", encoding="utf-8")
    assert workspace.safe_resolve(root, "sub/./b.txt") == (root / "sub" / "b.txt").resolve()


# --- 內部 symlink：放行並解析到真實目標 ---


def test_internal_file_symlink_resolves_to_real(root):
    real = root / "real.txt"
    real.write_text("R", encoding="utf-8")
    (root / "alias.txt").symlink_to(real)
    result = workspace.safe_resolve(root, "alias.txt")
    assert result == real.resolve()
    assert result.is_relative_to(root.resolve())


def test_internal_symlink_to_subdir_file(root):
    (root / "sub").mkdir()
    real = root / "sub" / "deep.txt"
    real.write_text("D", encoding="utf-8")
    (root / "link.txt").symlink_to(real)
    assert workspace.safe_resolve(root, "link.txt") == real.resolve()


def test_internal_dir_symlink_then_file(root):
    (root / "sub").mkdir()
    (root / "sub" / "x.txt").write_text("X", encoding="utf-8")
    (root / "alias").symlink_to(root / "sub")  # 目錄 symlink，指回內部
    result = workspace.safe_resolve(root, "alias/x.txt")
    assert result == (root / "sub" / "x.txt").resolve()
    assert result.is_relative_to(root.resolve())


def test_must_exist_false_new_file_exact_path(root):
    # 寫新檔場景：尚未存在也回正確的（未來）路徑
    result = workspace.safe_resolve(root, "new/file.txt", must_exist=False)
    assert result == (root / "new" / "file.txt").resolve()


def test_returned_path_is_usable_for_read(root):
    # 回傳 Path 可直接用於讀檔，內容正確（端到端正確性）
    (root / "c.txt").write_text("hello-12", encoding="utf-8")
    p = workspace.safe_resolve(root, "c.txt")
    assert p.read_text(encoding="utf-8") == "hello-12"
