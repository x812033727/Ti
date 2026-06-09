"""任務 #1 驗證：環境就緒並啟動服務（python -m studio.server，:8000 可開啟）。

對齊驗收標準 #1：服務啟動無錯誤，瀏覽器可開啟首頁與設定頁。
做法：真實以子程序啟動 `python -m studio.server`（非僅 TestClient），
輪詢 :8000 確認服務就緒，再驗證首頁 HTML、登入頁、設定 API 可達。
跑前備份 .env，跑後還原，避免污染既有金鑰。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
ENV = ROOT / ".env"
HOST = "127.0.0.1"
PORT = 8000
BASE = f"http://{HOST}:{PORT}"


def _get(path: str, timeout: float = 3.0):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


@pytest.fixture(scope="module")
def server():
    # 備份 .env，確保啟動不依賴/不污染既有設定。
    backup = ENV.read_bytes() if ENV.exists() else None

    env = dict(os.environ)
    env.pop("TI_ACCESS_PASSWORD", None)  # 確保以「門禁停用」狀態啟動（首次設定路徑）
    env["TI_HOST"] = HOST
    env["TI_PORT"] = str(PORT)

    proc = subprocess.Popen(
        [sys.executable, "-m", "studio.server"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # 輪詢直到 :8000 回應或逾時。
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:  # 提早退出＝啟動失敗
                break
            try:
                status, _ = _get("/api/health")
                if status == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.4)
        if not ready:
            out = ""
            if proc.poll() is not None and proc.stdout:
                out = proc.stdout.read()
            pytest.fail(f"服務未能在 :{PORT} 就緒。程序輸出：\n{out}")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if backup is not None:
            ENV.write_bytes(backup)


def test_pip_install_editable_importable():
    """pip install -e . 後 studio 套件可匯入（環境就緒）。"""
    import studio  # noqa: F401
    import studio.server  # noqa: F401


def test_homepage_opens(server):
    """驗收 #1：首頁可開啟，回 200 且為 HTML。"""
    status, body = _get("/")
    assert status == 200
    assert "<html" in body.lower() or "<!doctype" in body.lower()


def test_login_page_opens(server):
    """登入頁可開啟（門禁停用時首頁不導向，但 /login 仍應可達）。"""
    status, body = _get("/login")
    assert status == 200
    assert "<html" in body.lower() or "<!doctype" in body.lower()


def test_health_ok(server):
    """健康檢查回 ok=True。"""
    import json

    status, body = _get("/api/health")
    assert status == 200
    data = json.loads(body)
    assert data.get("ok") is True


def test_settings_page_reachable(server):
    """驗收 #1：設定頁資料來源 /api/settings 可達（門禁停用時直接放行）。"""
    import json

    status, body = _get("/api/settings")
    assert status == 200
    data = json.loads(body)
    assert "fields" in data and isinstance(data["fields"], list) and data["fields"]


def test_static_assets_served(server):
    """設定頁前端資源（app.js）可由靜態路由取得，瀏覽器才渲染得出設定 UI。"""
    status, _ = _get("/static/app.js")
    assert status == 200


def test_no_startup_error(server):
    """服務啟動後仍存活（無啟動即崩潰）。"""
    assert server.poll() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
