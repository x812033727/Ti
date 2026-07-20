"""服務啟動煙霧測試（合併前閘門的一部分）。

擋掉「過 lint + 單元測試，卻會讓服務根本起不來」的改動：import 服務進入點本身就是
一道 import 健檢，再用 TestClient 打不需認證的 /api/health 確認回 200。
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_server_imports_and_health_ok(monkeypatch):
    from studio import routes
    from studio.server import app  # import 進入點 → 抓語法/import 錯誤

    async def current_head(_repo_dir):
        return "a" * 40

    monkeypatch.setattr(routes.deploy, "current_head", current_head)
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["git_sha"] == "a" * 40


def test_health_fails_closed_on_unparseable_deploy_revision(monkeypatch):
    from studio import routes
    from studio.server import app

    async def current_head(_repo_dir):
        return "fatal: not a git repository"

    monkeypatch.setattr(routes.deploy, "current_head", current_head)
    assert TestClient(app).get("/api/health").json()["git_sha"] == "unknown"
