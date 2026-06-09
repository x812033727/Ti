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


def test_zip_keeps_internal_symlink(root):
    # symlink 指回 workspace 內 → 應保留（放行）。
    (root / "real.py").write_text("ok\n", encoding="utf-8")
    (root / "alias.py").symlink_to(root / "real.py")
    names = _names(workspace.zip_workspace("qa-sess"))
    assert "real.py" in names
    assert "alias.py" in names


# --- safe_resolve 單元測試：containment 真實來源的 5 類邊界 ---


def test_safe_resolve_rejects_dotdot(tmp_path):
    assert workspace.safe_resolve(tmp_path, "../evil.txt") is None
    assert workspace.safe_resolve(tmp_path, "a/../../evil.txt") is None


def test_safe_resolve_rejects_absolute(tmp_path):
    assert workspace.safe_resolve(tmp_path, "/etc/passwd") is None


def test_safe_resolve_rejects_missing_when_must_exist(tmp_path):
    # 不存在 → 回 None 而非丟例外
    assert workspace.safe_resolve(tmp_path, "nope.txt") is None


def test_safe_resolve_allows_missing_when_not_must_exist(tmp_path):
    # 寫新檔場景：尚未存在也放行
    target = workspace.safe_resolve(tmp_path, "new/file.txt", must_exist=False)
    assert target == (tmp_path / "new" / "file.txt").resolve()


def test_safe_resolve_external_symlink_blocked(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "leak").symlink_to(outside)
    assert workspace.safe_resolve(ws, "leak") is None


def test_safe_resolve_internal_symlink_allowed(tmp_path):
    (tmp_path / "real.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    assert workspace.safe_resolve(tmp_path, "link.txt") == (tmp_path / "real.txt").resolve()


def test_safe_resolve_symlink_loop_returns_none(tmp_path):
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    assert workspace.safe_resolve(tmp_path, "a") is None


def test_safe_resolve_target_equals_root(tmp_path):
    assert workspace.safe_resolve(tmp_path, "") == tmp_path.resolve()


def test_safe_resolve_write_through_existing_parent_symlink_blocked(tmp_path):
    """root 下放指向外部的 symlink 目錄、往其中寫『新檔』，仍應回 None：
    resolve(strict=False) 會展開『已存在』的前綴 symlink，故此逃逸被擋。
    （已知缺口僅限前綴 symlink『尚不存在』的尾段情形，見 safe_resolve docstring。）"""
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "linkdir").symlink_to(outside)  # 外部目錄 symlink，已存在
    assert workspace.safe_resolve(ws, "linkdir/new.txt", must_exist=False) is None
