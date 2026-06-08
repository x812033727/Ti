"""任務 #1「成果一鍵打包 zip 下載」驗收測試。

涵蓋 HTTP 端點 GET /api/workspace/{session_id}/download 的整常與邊界情況，
以及 workspace.zip_bytes 既有單元測試未涵蓋的補強案例（巢狀結構、安全檔名、合法 zip 格式）。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from studio import config, workspace


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    return tmp_path / "ws"


@pytest.fixture
def client(monkeypatch):
    """門禁停用下的 TestClient（向後相容預設）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    from studio.server import app

    return TestClient(app)


# --- HTTP 端點：正常下載 ----------------------------------------------
def test_download_returns_valid_zip(ws_root, client):
    root = workspace.create_workspace("demo")
    (root / "main.py").write_text("print('hi')", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "util.py").write_text("x = 1", encoding="utf-8")

    resp = client.get("/api/workspace/demo/download")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'attachment; filename="demo.zip"' in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.testzip() is None  # 合法 zip、無毀損
        assert set(zf.namelist()) == {"main.py", "src/util.py"}
        assert zf.read("main.py").decode() == "print('hi')"
        # 確認以 DEFLATE 壓縮
        assert zf.getinfo("main.py").compress_type == zipfile.ZIP_DEFLATED


# --- HTTP 端點：排除雜訊目錄 ------------------------------------------
def test_download_excludes_noise_dirs(ws_root, client):
    root = workspace.create_workspace("noisy")
    (root / "app.py").write_text("ok", encoding="utf-8")
    for noise in (".git", "__pycache__", "node_modules", ".venv"):
        (root / noise).mkdir()
        (root / noise / "junk").write_text("x", encoding="utf-8")

    resp = client.get("/api/workspace/noisy/download")

    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
    assert names == ["app.py"]


# --- HTTP 端點：不存在 / 空 → 404 ------------------------------------
def test_download_missing_workspace_404(ws_root, client):
    resp = client.get("/api/workspace/ghost/download")
    assert resp.status_code == 404


def test_download_empty_workspace_404(ws_root, client):
    workspace.create_workspace("empty")
    resp = client.get("/api/workspace/empty/download")
    assert resp.status_code == 404


# --- HTTP 端點：門禁啟用且未登入 → 401 -------------------------------
def test_download_requires_auth_when_enabled(ws_root, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    root = workspace.create_workspace("protected")
    (root / "a.py").write_text("ok", encoding="utf-8")

    from studio.server import app

    resp = TestClient(app).get("/api/workspace/protected/download")
    assert resp.status_code == 401


# --- 補強：深層巢狀結構保留 -------------------------------------------
def test_zip_keeps_deep_nested_structure(ws_root):
    root = workspace.create_workspace("deep")
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_text("nested", encoding="utf-8")

    data = workspace.zip_bytes("deep")
    assert data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["a/b/c/deep.txt"]
        assert zf.read("a/b/c/deep.txt").decode() == "nested"


# --- 補強：危險 session_id 檔名被消毒 --------------------------------
def test_download_sanitizes_filename(ws_root, client):
    # session_id 含危險字元：路由產生的下載檔名須消毒成僅含安全字元
    sid = "proj.v2.beta"  # 點號為 URL 合法路徑字元，但會被檔名消毒移除
    root = workspace.create_workspace(sid)
    (root / "f.py").write_text("ok", encoding="utf-8")

    resp = client.get(f"/api/workspace/{sid}/download")
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    fname = cd.split('filename="')[1].split('"')[0]
    # 檔名僅含英數 / - / _ 與 .zip
    stem = fname[: -len(".zip")]
    assert fname.endswith(".zip")
    assert all(c.isalnum() or c in "-_" for c in stem)
    assert stem == "projv2beta"


# --- 補強：含中文與 UTF-8 內容檔案 -----------------------------------
def test_zip_handles_utf8_content(ws_root):
    root = workspace.create_workspace("utf8")
    (root / "讀我.md").write_text("中文內容 🚀", encoding="utf-8")

    data = workspace.zip_bytes("utf8")
    assert data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "讀我.md" in names
        assert zf.read("讀我.md").decode("utf-8") == "中文內容 🚀"
