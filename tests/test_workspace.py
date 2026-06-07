"""workspace.zip_bytes 的單元測試：打包正確、排除雜訊、空/不存在回 None。"""

from __future__ import annotations

import io
import zipfile

import pytest

from studio import config, workspace


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    return tmp_path / "ws"


def test_zip_bytes_packs_files_and_keeps_structure(ws_root):
    root = workspace.create_workspace("sess1")
    (root / "main.py").write_text("print('hi')", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "util.py").write_text("x = 1", encoding="utf-8")

    data = workspace.zip_bytes("sess1")
    assert data is not None

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert names == {"main.py", "pkg/util.py"}
        assert zf.read("main.py").decode() == "print('hi')"


def test_zip_bytes_excludes_noise_dirs(ws_root):
    root = workspace.create_workspace("sess2")
    (root / "app.py").write_text("ok", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret", encoding="utf-8")

    data = workspace.zip_bytes("sess2")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert names == ["app.py"]
    assert all(".git" not in n for n in names)


def test_zip_bytes_missing_workspace_returns_none(ws_root):
    assert workspace.zip_bytes("nope") is None


def test_zip_bytes_empty_workspace_returns_none(ws_root):
    workspace.create_workspace("empty")
    assert workspace.zip_bytes("empty") is None
