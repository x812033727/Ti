"""專案 API 的藍圖與優先級擴充：detail 回傳藍圖＋排序 backlog、POST 透傳 priority/type。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import blueprint, config, projects


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → require_auth 放行
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    from studio.server import app

    return TestClient(app)


def test_detail_includes_blueprint_and_sorted_backlog(client, monkeypatch):
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", True)
    pid = projects.create("產品X", vision="願景")["id"]
    blueprint.save(
        pid, {"version": 1, "vision": "願景", "features": [{"title": "F", "priority": 0}]}
    )
    # 先排 P1，再排 P0 → 回傳順序應 P0 先（依消化順序，前端不必自己排）。
    client.post(f"/api/projects/{pid}/backlog", json={"title": "普通改良"})
    client.post(
        f"/api/projects/{pid}/backlog", json={"title": "緊急修復", "priority": 0, "type": "bug"}
    )

    data = client.get(f"/api/projects/{pid}").json()
    assert data["blueprint"]["features"][0]["title"] == "F"
    titles = [t["title"] for t in data["backlog"]]
    assert titles == ["緊急修復", "普通改良"]
    first = data["backlog"][0]
    assert first["priority"] == 0 and first["type"] == "bug" and first["source"] == "user"


def test_detail_without_blueprint_is_null(client):
    pid = projects.create("無藍圖")["id"]
    data = client.get(f"/api/projects/{pid}").json()
    assert data["blueprint"] is None


def test_add_task_clamps_priority_and_normalizes_type(client):
    pid = projects.create("夾值")["id"]
    res = client.post(
        f"/api/projects/{pid}/backlog", json={"title": "任務", "priority": 9, "type": "怪型別"}
    )
    task = res.json()["task"]
    assert task["priority"] == 2 and task["type"] == "improvement"


def test_add_task_defaults_unchanged(client):
    # 舊呼叫端（只給 title/detail）行為不變：P1 / improvement。
    pid = projects.create("預設")["id"]
    task = client.post(f"/api/projects/{pid}/backlog", json={"title": "舊格式"}).json()["task"]
    assert task["priority"] == 1 and task["type"] == "improvement"


def test_add_task_truncates_long_detail(client):
    pid = projects.create("長細節")["id"]
    client.post(
        f"/api/projects/{pid}/backlog",
        json={"title": "長細節任務", "detail": "x" * 5000},
    )

    data = client.get(f"/api/projects/{pid}").json()
    task = next(t for t in data["backlog"] if t["title"] == "長細節任務")
    assert len(task["detail"]) <= 4000
