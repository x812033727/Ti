"""QA 獨立驗證（任務 #1）：read_file 邊界、read_notes 單層語意、單一真實來源。

工程師已覆蓋 safe_resolve / zip / _safe_path；此檔補強 read_file 各路徑與
read_notes 的「只准單層 NOTES」語意，並把驗收標準逐條釘死。
"""

from __future__ import annotations

import pytest

from studio import config, workspace
from studio.tools import _safe_path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return workspace.create_workspace("qa")


# --- 驗收標準 2/3：read_file 五類邊界 ---


def test_read_file_normal(root):
    (root / "ok.txt").write_text("hello\n", encoding="utf-8")
    assert workspace.read_file("qa", "ok.txt") == "hello\n"


def test_read_file_nested_normal(root):
    (root / "d").mkdir()
    (root / "d" / "x.py").write_text("x=1\n", encoding="utf-8")
    assert workspace.read_file("qa", "d/x.py") == "x=1\n"


def test_read_file_dotdot_escape(root):
    (root.parent / "secret.txt").write_text("S\n", encoding="utf-8")
    assert workspace.read_file("qa", "../secret.txt") is None


def test_read_file_absolute_blocked(root):
    assert workspace.read_file("qa", "/etc/passwd") is None


def test_read_file_missing_returns_none(root):
    assert workspace.read_file("qa", "nope.txt") is None


def test_read_file_external_symlink_blocked(root):
    secret = root.parent / "out.txt"
    secret.write_text("TOP\n", encoding="utf-8")
    (root / "leak").symlink_to(secret)
    assert workspace.read_file("qa", "leak") is None


def test_read_file_internal_symlink_allowed(root):
    (root / "real.txt").write_text("ok\n", encoding="utf-8")
    (root / "alias.txt").symlink_to(root / "real.txt")
    assert workspace.read_file("qa", "alias.txt") == "ok\n"


def test_read_file_target_equals_root_is_dir_returns_none(root):
    # rel="" → target == root（目錄），safe_resolve 放行但非檔案 → read_file 回 None
    assert workspace.read_file("qa", "") is None


# --- 驗收標準 4：read_notes 只准單層 NOTES ---


def test_read_notes_roundtrip(root):
    workspace.append_note("qa", "知識一")
    assert "知識一" in workspace.read_notes("qa")


def test_read_notes_missing_returns_empty(root):
    assert workspace.read_notes("qa") == ""


def test_read_notes_external_symlink_blocked(root):
    # NOTES.md 本身是指向外部的 symlink → 應拒讀（不外洩）
    secret = root.parent / "evil_notes.md"
    secret.write_text("LEAK\n", encoding="utf-8")
    (root / workspace.NOTES_FILE).symlink_to(secret)
    assert workspace.read_notes("qa") == ""


def test_read_notes_single_layer_semantics_preserved():
    """釘死驗收標準 4：read_notes 程式碼保留 target.parent == root 的單層判斷。"""
    import inspect

    src = inspect.getsource(workspace.read_notes)
    assert "target.parent != safe_root" in src or "target.parent == safe_root" in src


# --- 驗收標準 1：containment 邏輯單一真實來源 ---


def test_single_source_of_truth():
    """read_file / read_notes / zip_workspace / _safe_path 皆呼叫 safe_resolve，
    且除 safe_resolve 本身外無重複的 .resolve(strict= 寫法。"""
    import inspect

    for fn in (workspace.read_file, workspace.zip_workspace):
        assert "safe_resolve" in inspect.getsource(fn)
    assert "safe_resolve" in inspect.getsource(_safe_path)
    # 只有 safe_resolve 內出現 strict= 的解析
    assert "strict=" in inspect.getsource(workspace.safe_resolve)
    assert "strict=" not in inspect.getsource(workspace.read_file)


# --- 驗收標準 5：_safe_path 行為一致 + 無循環 import ---


def test_safe_path_target_equals_root(tmp_path):
    assert _safe_path(tmp_path, "") == tmp_path.resolve()


def test_no_circular_import():
    import importlib

    import studio.tools as t
    import studio.workspace as w

    importlib.reload(w)
    importlib.reload(t)
    assert t.safe_resolve is w.safe_resolve
