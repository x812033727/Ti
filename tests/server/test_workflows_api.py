"""`/api/workflows` CRUD 的 API 層測試（TestClient）。

涵蓋：完整 CRUD happy path、內建預設永遠可選、422（非法 stage type／角色／verdict／結構）、
409 同名（含保留預設名）、404 更新/刪除不存在、workflows.yaml 損壞回 500、未登入 401、
門禁停用時寫入退回僅限本機。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, workflow


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → 寫入退回 loopback fail-safe
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    from studio.server import app

    # 寫入端點掛 WRITE_DEPS(require_admin)：門禁停用時退回僅限本機，故用 loopback peer 放行。
    return TestClient(app, client=("127.0.0.1", 12345))


def _simple_stages():
    return [
        {"type": "decompose"},
        {"type": "build", "task_pipeline": [{"type": "implement", "assignee": "engineer"}]},
        {"type": "demo"},
    ]


def test_crud_happy_path(client):
    res = client.post("/api/workflows", json={"name": "快速原型", "stages": _simple_stages()})
    assert res.status_code == 200
    wf = res.json()["workflow"]
    assert wf["name"] == "快速原型" and [s["type"] for s in wf["stages"]] == [
        "decompose",
        "build",
        "demo",
    ]
    # 列出（含內建預設）
    res = client.get("/api/workflows")
    assert res.status_code == 200
    names = [w["name"] for w in res.json()["workflows"]]
    assert workflow.DEFAULT_WORKFLOW_NAME in names and "快速原型" in names
    # 更新（整筆替換）
    res = client.put(
        "/api/workflows/快速原型", json={"description": "改版", "stages": [{"type": "demo"}]}
    )
    assert res.status_code == 200 and res.json()["workflow"]["description"] == "改版"
    # 刪除
    assert client.delete("/api/workflows/快速原型").status_code == 200
    assert [w["name"] for w in client.get("/api/workflows").json()["workflows"]] == [
        workflow.DEFAULT_WORKFLOW_NAME
    ]


def test_builtin_default_always_listed(client):
    # 沒建任何 workflow 時，列表也含內建預設（UI 一律可選）。
    names = [w["name"] for w in client.get("/api/workflows").json()["workflows"]]
    assert names == [workflow.DEFAULT_WORKFLOW_NAME]


def test_invalid_stage_type_422(client):
    res = client.post("/api/workflows", json={"name": "w", "stages": [{"type": "teleport"}]})
    assert res.status_code == 422 and "不合法" in res.json()["error"]


def test_unknown_role_422(client):
    res = client.post(
        "/api/workflows", json={"name": "w", "stages": [{"type": "discuss", "roles": ["ghost"]}]}
    )
    assert res.status_code == 422 and "ghost" in res.json()["error"]


def test_bad_verdict_422(client):
    stages = [
        {
            "type": "build",
            "task_pipeline": [{"type": "review", "gate": [{"role": "qa", "verdict": "bogus"}]}],
        }
    ]
    res = client.post("/api/workflows", json={"name": "w", "stages": stages})
    assert res.status_code == 422 and "白名單" in res.json()["error"]


def test_build_without_pipeline_422(client):
    res = client.post("/api/workflows", json={"name": "w", "stages": [{"type": "build"}]})
    assert res.status_code == 422 and "task_pipeline" in res.json()["error"]


def test_duplicate_name_409(client):
    body = {"name": "同名", "stages": [{"type": "demo"}]}
    assert client.post("/api/workflows", json=body).status_code == 200
    res = client.post("/api/workflows", json=body)
    assert res.status_code == 409 and "已存在" in res.json()["error"]


def test_reserved_default_name_409(client):
    res = client.post(
        "/api/workflows",
        json={"name": workflow.DEFAULT_WORKFLOW_NAME, "stages": [{"type": "demo"}]},
    )
    assert res.status_code == 409


def test_update_validates_and_404(client):
    client.post("/api/workflows", json={"name": "w", "stages": [{"type": "demo"}]})
    res = client.put("/api/workflows/w", json={"stages": [{"type": "teleport"}]})
    assert res.status_code == 422
    res = client.put("/api/workflows/沒這個", json={"stages": [{"type": "demo"}]})
    assert res.status_code == 404


def test_delete_nonexistent_404(client):
    assert client.delete("/api/workflows/沒這個").status_code == 404


def test_corrupt_file_500(client):
    (config.ROLES_DIR / "workflows.yaml").write_text("workflows: [壞掉", encoding="utf-8")
    res = client.get("/api/workflows")
    assert res.status_code == 500 and "YAML" in res.json()["error"]


def test_requires_auth_when_enabled(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    assert client.get("/api/workflows").status_code == 401
    assert (
        client.post("/api/workflows", json={"name": "w", "stages": [{"type": "demo"}]}).status_code
        == 401
    )


def test_writes_loopback_only_when_auth_disabled(tmp_path, monkeypatch):
    """門禁停用時，寫入端點須掛 WRITE_DEPS(require_admin) 退回僅限本機（與 /api/groups 同級）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    from studio.server import app

    remote = TestClient(app, client=("203.0.113.7", 40000))  # 非本機
    body = {"name": "w", "stages": [{"type": "demo"}]}
    assert remote.post("/api/workflows", json=body).status_code == 403
    assert remote.put("/api/workflows/w", json={"stages": [{"type": "demo"}]}).status_code == 403
    assert remote.delete("/api/workflows/w").status_code == 403
    assert remote.get("/api/workflows").status_code == 200  # 讀取不受限
