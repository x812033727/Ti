"""QA 獨立驗證（任務 #6）：zip 迴圈對每個 entry 過 safe_resolve，擋 list_files 取到的逃逸 symlink。

關鍵前提：list_files() 只比相對路徑、不解析 symlink，所以它『會』把逃逸 symlink
列進清單——真正的把關落在 zip 迴圈的 safe_resolve。本檔建立 list_files↔zip 的
對照，證明：被 list_files 列出的逃逸 entry，最終不會進 zip，且合法檔不漏。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from studio import config, workspace


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return workspace.create_workspace("zip6")


def _names(data: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_list_files_includes_escaping_symlink_but_zip_excludes(root, tmp_path):
    """前提證明 + 結果對照：list_files 列出逃逸 symlink，zip 卻把它擋掉。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("PWNED_9Z\n", encoding="utf-8")
    (root / "keep.py").write_text("ok\n", encoding="utf-8")
    (root / "leak").symlink_to(secret)

    listed = workspace.list_files("zip6")
    # 前提：list_files 不解析 symlink，因此 leak 會在清單裡
    assert "leak" in listed
    assert "keep.py" in listed

    data = workspace.zip_workspace("zip6")
    names = _names(data)
    # 結果：zip 的 safe_resolve 把 leak 擋掉，但 keep.py 保留
    assert "leak" not in names
    assert "keep.py" in names
    assert b"PWNED_9Z" not in data


def test_internal_symlink_listed_and_zipped(root):
    """內部 symlink：list_files 列出、zip 也保留（放行）。"""
    (root / "real.py").write_text("R\n", encoding="utf-8")
    (root / "alias.py").symlink_to(root / "real.py")
    assert "alias.py" in workspace.list_files("zip6")
    names = _names(workspace.zip_workspace("zip6"))
    assert "alias.py" in names and "real.py" in names


def test_all_legit_files_survive(root):
    """純合法檔（含巢狀）全部進 zip，無誤殺。"""
    (root / "a.txt").write_text("A\n", encoding="utf-8")
    (root / "d").mkdir()
    (root / "d" / "b.txt").write_text("B\n", encoding="utf-8")
    listed = set(workspace.list_files("zip6"))
    names = set(_names(workspace.zip_workspace("zip6")))
    assert listed == names == {"a.txt", "d/b.txt"}


def test_mixed_legit_and_escaping(root, tmp_path):
    """混合場景：合法檔保留、外部 symlink（含子目錄裡的）全擋。"""
    out = tmp_path / "o.txt"
    out.write_text("LEAK\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("m\n", encoding="utf-8")
    (root / "src" / "escape").symlink_to(out)
    (root / "top_leak").symlink_to(out)

    data = workspace.zip_workspace("zip6")
    names = _names(data)
    assert "src/main.py" in names
    assert "src/escape" not in names
    assert "top_leak" not in names
    assert b"LEAK" not in data


def test_zip_delegates_to_safe_resolve():
    import inspect

    src = inspect.getsource(workspace.zip_workspace)
    assert "safe_resolve" in src
    assert "not in target.parents" not in src
