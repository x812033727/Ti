"""QA 獨立驗證（任務 #7）：read_notes 改呼叫 safe_resolve、不再自寫 resolve 邏輯、保留單層。

聚焦重構面向：
- containment（strict resolve + is_relative_to）已委派 safe_resolve，read_notes 內
  不再出現 strict resolve 與 containment 比對；
- 仍保留 target.parent == root 的單層判斷；
- 全 workspace.py 僅 safe_resolve 一處持有 strict resolve（單一真實來源）。
"""

from __future__ import annotations

import inspect

import pytest

from studio import config, workspace


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return workspace.create_workspace("rn7")


# --- 行為（驗收標準 4）---


def test_append_read_roundtrip(root):
    workspace.append_note("rn7", "知識A")
    assert "知識A" in workspace.read_notes("rn7")


def test_real_single_layer_file(root):
    (root / workspace.NOTES_FILE).write_text("筆記\n", encoding="utf-8")
    assert workspace.read_notes("rn7") == "筆記\n"


def test_missing_returns_empty(root):
    assert workspace.read_notes("rn7") == ""


def test_symlink_to_subdir_rejected_by_single_layer(root):
    """containment 成立（safe_resolve 放行）但落在子層 → 單層判斷拒讀。"""
    (root / "sub").mkdir()
    real = root / "sub" / "n.md"
    real.write_text("DEEP\n", encoding="utf-8")
    (root / workspace.NOTES_FILE).symlink_to(real)
    # safe_resolve 本身放行
    assert workspace.safe_resolve(root.resolve(), workspace.NOTES_FILE) == real.resolve()
    # read_notes 因單層判斷拒讀
    assert workspace.read_notes("rn7") == ""


def test_external_symlink_rejected(root):
    secret = root.parent / "evil.md"
    secret.write_text("LEAK\n", encoding="utf-8")
    (root / workspace.NOTES_FILE).symlink_to(secret)
    assert workspace.read_notes("rn7") == ""


# --- 重構（驗收標準 1）---


def test_read_notes_no_strict_resolve_inline():
    src = inspect.getsource(workspace.read_notes)
    assert "safe_resolve" in src
    # 不再自寫 strict resolve 與 containment 比對
    assert "strict=" not in src
    assert "is_relative_to" not in src
    # 仍保留單層判斷
    assert "target.parent != safe_root" in src


def test_safe_resolve_is_single_source_of_strict_resolve():
    """整個 workspace.py 內，strict resolve 只出現在 safe_resolve 一處。"""
    src = inspect.getsource(workspace)
    # 找出含 'strict=' 的行所屬函式：唯一允許者為 safe_resolve
    strict_lines = [ln for ln in src.splitlines() if "strict=" in ln]
    assert len(strict_lines) >= 1
    only_in_safe_resolve = inspect.getsource(workspace.safe_resolve)
    for ln in strict_lines:
        assert ln in only_in_safe_resolve, f"strict resolve 外洩到 safe_resolve 之外: {ln}"
