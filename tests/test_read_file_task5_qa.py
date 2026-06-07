"""QA 獨立驗證（任務 #5）：read_file 改呼叫 safe_resolve、移除 inline 檢查。

釘死：read_file 不再自寫 containment 比對（原 `not in target.parents`/`target != root`），
完整覆蓋 5 類邊界 + 不存在/symlink loop/target==root(目錄)/巢狀合法。
"""

from __future__ import annotations

import pytest

from studio import config, workspace


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return workspace.create_workspace("rf-qa")


# --- 正常 ---


def test_read_normal(root):
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    assert workspace.read_file("rf-qa", "a.txt") == "hello\n"


def test_read_nested(root):
    (root / "pkg").mkdir()
    (root / "pkg" / "m.py").write_text("x=1\n", encoding="utf-8")
    assert workspace.read_file("rf-qa", "pkg/m.py") == "x=1\n"


# --- 5 類邊界 ---


def test_dotdot_escape_blocked(root):
    (root.parent / "secret.txt").write_text("S\n", encoding="utf-8")
    assert workspace.read_file("rf-qa", "../secret.txt") is None
    assert workspace.read_file("rf-qa", "pkg/../../secret.txt") is None


def test_absolute_blocked(root):
    assert workspace.read_file("rf-qa", "/etc/passwd") is None


def test_external_symlink_blocked(root):
    secret = root.parent / "out.txt"
    secret.write_text("LEAK\n", encoding="utf-8")
    (root / "leak").symlink_to(secret)
    assert workspace.read_file("rf-qa", "leak") is None


def test_internal_symlink_allowed(root):
    (root / "real.txt").write_text("OK\n", encoding="utf-8")
    (root / "alias.txt").symlink_to(root / "real.txt")
    assert workspace.read_file("rf-qa", "alias.txt") == "OK\n"


def test_target_equals_root_dir_returns_none(root):
    # rel="" → target==root（目錄），safe_resolve 放行但非檔案 → None
    assert workspace.read_file("rf-qa", "") is None


# --- 其餘 ---


def test_missing_returns_none(root):
    assert workspace.read_file("rf-qa", "nope.txt") is None


def test_symlink_loop_returns_none(root):
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")
    assert workspace.read_file("rf-qa", "a") is None


def test_missing_session_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    assert workspace.read_file("no-such-session", "a.txt") is None


# --- 標準 1：委派 safe_resolve、無 inline 比對 ---


def test_read_file_delegates_to_safe_resolve():
    import inspect

    src = inspect.getsource(workspace.read_file)
    assert "safe_resolve" in src
    assert "not in target.parents" not in src
    assert "target != root" not in src
    assert ".resolve(" not in src  # inline 解析已移除
