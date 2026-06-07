"""QA 獨立驗證（任務 #2）：read_notes 的「只准單層 NOTES」語意。

核心：safe_resolve 只負責 containment（落在 root 內就放行），單層限制
（target.parent == root，更深層拒絕）由 read_notes 額外把關。本檔釘死這條
分工——尤其是「symlink 指向 workspace 內子目錄」這條 safe_resolve 放行、
但 read_notes 必須拒讀的邊界。
"""

from __future__ import annotations

import pytest

from studio import config, workspace


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return workspace.create_workspace("notes-qa")


# --- 正常路徑 ---


def test_append_then_read_roundtrip(root):
    workspace.append_note("notes-qa", "第一條知識")
    workspace.append_note("notes-qa", "第二條知識")
    out = workspace.read_notes("notes-qa")
    assert "第一條知識" in out
    assert "第二條知識" in out


def test_read_notes_missing_returns_empty(root):
    assert workspace.read_notes("notes-qa") == ""


def test_read_notes_real_single_layer_file(root):
    (root / workspace.NOTES_FILE).write_text("純文字筆記\n", encoding="utf-8")
    assert workspace.read_notes("notes-qa") == "純文字筆記\n"


# --- 驗收標準 4 核心：更深層 / 逃逸一律拒讀 ---


def test_read_notes_rejects_symlink_to_inner_subdir(root):
    """NOTES.md 是 symlink，指向 workspace 內『子目錄』的檔案：
    containment 成立（safe_resolve 放行），但解析後 target.parent != root，
    read_notes 的單層判斷必須拒讀 → 回 ''。"""
    (root / "sub").mkdir()
    real = root / "sub" / "real_notes.md"
    real.write_text("DEEP\n", encoding="utf-8")
    (root / workspace.NOTES_FILE).symlink_to(real)

    # 先確認 safe_resolve 本身是放行的（containment OK），證明擋下來自單層判斷
    resolved = workspace.safe_resolve(root.resolve(), workspace.NOTES_FILE)
    assert resolved == real.resolve()
    assert resolved.parent != root.resolve()  # 落在子層

    # read_notes 必須拒讀
    assert workspace.read_notes("notes-qa") == ""


def test_read_notes_rejects_external_symlink(root):
    """NOTES.md 指向 workspace 外 → safe_resolve 直接擋 → 回 ''。"""
    secret = root.parent / "evil_notes.md"
    secret.write_text("LEAK\n", encoding="utf-8")
    (root / workspace.NOTES_FILE).symlink_to(secret)
    assert workspace.read_notes("notes-qa") == ""


def test_read_notes_dir_symlink_to_inner_then_notes(root):
    """root/NOTES.md 經由『目錄 symlink』落到子層也應拒：
    建 root/alias -> root/sub，NOTES.md -> alias/real.md，解析後 parent 仍非 root。"""
    (root / "sub").mkdir()
    real = root / "sub" / "x.md"
    real.write_text("Y\n", encoding="utf-8")
    (root / "alias").symlink_to(root / "sub")
    (root / workspace.NOTES_FILE).symlink_to(root / "alias" / "x.md")
    assert workspace.read_notes("notes-qa") == ""


# --- 驗收標準 1：read_notes 不再自寫 resolve，改呼叫 safe_resolve ---


def test_read_notes_delegates_to_safe_resolve():
    import inspect

    src = inspect.getsource(workspace.read_notes)
    assert "safe_resolve" in src
    # 不再自寫 (root/NOTES_FILE).resolve() 這類 inline 解析
    assert ".resolve(strict=" not in src
    # 仍保留單層判斷
    assert "target.parent != safe_root" in src
