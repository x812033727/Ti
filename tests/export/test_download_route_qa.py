"""QA 補強：/api/workspace/{id}/download 路由驗證（任務 #2）。"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from studio import auth, config, workspace


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "sessQA-1"
    root = workspace.create_workspace(sid)
    (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("y=2\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")
    return sid


@pytest.fixture
def client():
    from studio.server import app

    return TestClient(app)


def test_ok_zip_headers_and_content(client, session, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    res = client.get(f"/api/workspace/{session}/download")
    assert res.status_code == 200
    # 驗收 #1：合法 zip 的 Content-Type 與 attachment
    assert res.headers["content-type"] in ("application/zip", "application/octet-stream")
    cd = res.headers["content-disposition"]
    assert "attachment" in cd
    # 驗收 #5：檔名含 session_id
    assert session in cd
    # 驗收 #2：內容正確、不含 .git
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    assert zf.testzip() is None
    names = zf.namelist()
    assert "app.py" in names
    assert "pkg/mod.py" in names
    assert not any(n.startswith(".git") for n in names)


def test_missing_session_404(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    assert client.get("/api/workspace/ghost/download").status_code == 404


def test_path_traversal_404_no_leak(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    res = client.get("/api/workspace/..%2f..%2fetc/download")
    assert res.status_code in (400, 404)
    # 確保沒回傳任何 zip
    assert res.headers.get("content-type") != "application/zip"


def test_unauth_blocked_when_gated(client, session, monkeypatch):
    # 驗收 #4：啟用門禁、未登入 → 401
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    res = client.get(f"/api/workspace/{session}/download")
    assert res.status_code == 401


def test_authed_cookie_allows_download_when_gated(client, session, monkeypatch):
    # 驗收 #4 反面：門禁啟用、帶有效 cookie → 200，證明 require_auth 真的接上且能放行
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client.cookies.set(config.AUTH_COOKIE, auth.make_token())
    res = client.get(f"/api/workspace/{session}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] in ("application/zip", "application/octet-stream")


def test_bad_cookie_blocked_when_gated(client, session, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client.cookies.set(config.AUTH_COOKIE, "garbage.token")
    res = client.get(f"/api/workspace/{session}/download")
    assert res.status_code == 401
