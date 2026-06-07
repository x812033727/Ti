"""QA 獨立驗證（任務 #3）：zip_workspace 對每個 entry 過 safe_resolve，擋逃逸 symlink。

list_files() 會把 symlink 也列出來（它只比相對路徑、不解析 symlink），所以真正
擋逃逸的責任落在 zip 迴圈裡的 safe_resolve。本檔釘死：外部 symlink 的內容絕不
進 zip、子目錄/巢狀逃逸一樣擋、內部 symlink 放行、合法內容完整。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from studio import config, workspace


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return workspace.create_workspace("zip-qa")


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


SECRET = "TOP_SECRET_DO_NOT_LEAK_8F3A"


def test_external_symlink_content_never_in_zip_bytes(root, tmp_path):
    """不僅是檔名被排除——外部秘密檔的『內容』也絕不出現在 zip 原始 bytes 中。"""
    secret = tmp_path / "passwd"
    secret.write_text(SECRET + "\n", encoding="utf-8")
    (root / "real.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "leak").symlink_to(secret)

    data = workspace.zip_workspace("zip-qa")
    names = _zip(data).namelist()
    assert "real.py" in names
    assert "leak" not in names
    # 直接掃 zip 原始 bytes，確認秘密內容沒被打包進去
    assert SECRET.encode() not in data


def test_symlink_in_subdir_escaping_blocked(root, tmp_path):
    """逃逸 symlink 藏在子目錄裡也要擋。"""
    secret = tmp_path / "outside.txt"
    secret.write_text(SECRET + "\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("x=1\n", encoding="utf-8")
    (root / "pkg" / "leak").symlink_to(secret)

    data = workspace.zip_workspace("zip-qa")
    names = _zip(data).namelist()
    assert "pkg/mod.py" in names
    assert "pkg/leak" not in names
    assert SECRET.encode() not in data


def test_external_dir_symlink_blocked(root, tmp_path):
    """指向外部『目錄』的 symlink，其下檔案會被 list_files 列出，但須整批擋下。"""
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "a.txt").write_text(SECRET + "\n", encoding="utf-8")
    (root / "keep.py").write_text("ok\n", encoding="utf-8")
    (root / "linkdir").symlink_to(outside)

    data = workspace.zip_workspace("zip-qa")
    names = _zip(data).namelist()
    assert "keep.py" in names
    assert not any(n.startswith("linkdir") for n in names)
    assert SECRET.encode() not in data


def test_internal_symlink_kept_and_content_correct(root):
    """symlink 指回 workspace 內 → 放行，且內容正確。"""
    (root / "real.py").write_text("REAL\n", encoding="utf-8")
    (root / "alias.py").symlink_to(root / "real.py")
    data = workspace.zip_workspace("zip-qa")
    zf = _zip(data)
    assert zf.testzip() is None
    assert "alias.py" in zf.namelist()
    assert zf.read("alias.py").decode() == "REAL\n"


def test_zip_delegates_to_safe_resolve():
    import inspect

    src = inspect.getsource(workspace.zip_workspace)
    assert "safe_resolve" in src
    # zip 迴圈不再自寫 containment 比對（safe_root not in target.parents）
    assert "not in target.parents" not in src
