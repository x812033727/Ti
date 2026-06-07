"""workspace 成果匯出下載測試：打包內容正確、排除 .git、門禁保護。"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from studio import config, workspace


@pytest.fixture
def session(tmp_path, monkeypatch):
    """建立一個有產出（含 .git 雜訊）的 workspace，回傳 session_id。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "demo123"
    root = workspace.create_workspace(sid)
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "util.py").write_text("x = 1\n", encoding="utf-8")
    # 應被排除的雜訊
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return sid


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


# --- 單元：zip_workspace ------------------------------------------------
def test_zip_contains_outputs_excludes_git(session):
    data = workspace.zip_workspace(session)
    assert data is not None
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "main.py" in names
    assert "sub/util.py" in names
    assert not any(n.startswith(".git") for n in names)


def test_zip_skips_symlink_escaping_sandbox(session, tmp_path):
    # 在沙箱外放一個秘密檔，於 workspace 內以 symlink 指向它。
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")
    link = workspace.workspace_path(session) / "leak.txt"
    link.symlink_to(secret)
    data = workspace.zip_workspace(session)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "leak.txt" not in names  # 逃逸的 symlink 不被打包
    assert "main.py" in names


def test_zip_missing_session_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    assert workspace.zip_workspace("nope") is None


# --- 共用知識庫 NOTES.md ----------------------------------------------
def test_notes_append_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "notessess"
    workspace.create_workspace(sid)
    assert workspace.read_notes(sid) == ""  # 尚未寫入
    workspace.append_note(sid, "踩到的坑：除數為 0 要報錯")
    workspace.append_note(sid, "決策：核心與介面分檔")
    notes = workspace.read_notes(sid)
    assert "除數為 0" in notes
    assert "核心與介面分檔" in notes
    # 空白不寫入
    workspace.append_note(sid, "   ")
    assert workspace.read_notes(sid).count("決策") == 1


def test_notes_excluded_from_files_and_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "noteszip"
    root = workspace.create_workspace(sid)
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    workspace.append_note(sid, "一些跨任務知識")
    # 不進檔案清單
    assert "NOTES.md" not in workspace.list_files(sid)
    assert "main.py" in workspace.list_files(sid)
    # 不進打包
    data = workspace.zip_workspace(sid)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "NOTES.md" not in names
    assert "main.py" in names


# --- 路由：下載 --------------------------------------------------------
def test_download_route_returns_zip(app, session, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    client = TestClient(app)
    res = client.get(f"/api/workspace/{session}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert "attachment" in res.headers["content-disposition"]
    assert session in res.headers["content-disposition"]
    # 回應為合法 zip
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert "main.py" in names
    assert not any(n.startswith(".git") for n in names)


def test_download_missing_session_404(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    client = TestClient(app)
    assert client.get("/api/workspace/ghost/download").status_code == 404


def test_download_path_traversal_does_not_leak(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    client = TestClient(app)
    # 含 ../ 的 session_id 不會對應到任何 workspace，回 404，不外洩沙箱外檔案。
    res = client.get("/api/workspace/..%2f..%2fetc/download")
    assert res.status_code in (400, 404)


def test_download_requires_auth_when_gated(app, session, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app)
    assert client.get(f"/api/workspace/{session}/download").status_code == 401
