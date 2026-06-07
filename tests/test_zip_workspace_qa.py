"""QA 補強：zip_workspace 邊界驗證（任務 #1）。"""

from __future__ import annotations

import io
import zipfile

import pytest

from studio import config, workspace


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return workspace.create_workspace("qa-sess")


def _names(data: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_excludes_all_noise_dirs(root):
    (root / "keep.py").write_text("x=1\n", encoding="utf-8")
    for noise in (".git", "__pycache__", "node_modules", ".venv", "venv"):
        (root / noise).mkdir()
        (root / noise / "junk").write_text("junk\n", encoding="utf-8")
    names = _names(workspace.zip_workspace("qa-sess"))
    assert "keep.py" in names
    for noise in (".git", "__pycache__", "node_modules", ".venv", "venv"):
        assert not any(n.startswith(noise) for n in names), noise


def test_zip_is_valid_and_content_matches(root):
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    (root / "deep").mkdir()
    (root / "deep" / "b.txt").write_text("world\n", encoding="utf-8")
    data = workspace.zip_workspace("qa-sess")
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None  # CRC 全部正確 = 合法 zip
    assert zf.read("a.txt").decode() == "hello\n"
    assert zf.read("deep/b.txt").decode() == "world\n"


def test_empty_workspace_returns_none(root):
    # 只有雜訊、沒有實際產出 → 視為無內容
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x\n", encoding="utf-8")
    assert workspace.zip_workspace("qa-sess") is None


def test_missing_session_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    assert workspace.zip_workspace("does-not-exist") is None


def test_traversal_session_id_sanitized_no_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    # workspace_path 會過濾掉 ../ 與斜線，不會指向 WORKSPACE_ROOT 之外
    p = workspace.workspace_path("../../etc/passwd")
    assert tmp_path.resolve() in p.resolve().parents
    # 對應的 workspace 不存在 → None，不外洩
    assert workspace.zip_workspace("../../etc/passwd") is None


def test_zip_excludes_symlink_escaping_sandbox(root, tmp_path):
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")
    (root / "real.py").write_text("ok\n", encoding="utf-8")
    (root / "leak").symlink_to(secret)
    names = _names(workspace.zip_workspace("qa-sess"))
    assert "real.py" in names
    assert "leak" not in names
