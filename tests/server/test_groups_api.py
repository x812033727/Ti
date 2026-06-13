"""`/api/groups` CRUD 的 API 層測試（TestClient）。

涵蓋：完整 CRUD happy path、四種 422（引用不存在角色／重複 key／僅 1 人／非法 mode）、
409 同名、404 更新/刪除不存在、groups.yaml 損壞回 500、未登入 401。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → 寫入退回 loopback fail-safe
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    from studio.server import app

    # 寫入端點掛 WRITE_DEPS(require_admin)：門禁停用時退回僅限本機，故用 loopback peer 放行。
    return TestClient(app, client=("127.0.0.1", 12345))


def test_crud_happy_path(client):
    # 建立
    res = client.post(
        "/api/groups",
        json={"name": "評審組", "role_keys": ["engineer", "senior"], "mode": "round_robin"},
    )
    assert res.status_code == 200
    assert res.json()["group"] == {
        "name": "評審組",
        "role_keys": ["engineer", "senior"],
        "mode": "round_robin",
    }
    # 列出
    res = client.get("/api/groups")
    assert res.status_code == 200
    assert [g["name"] for g in res.json()["groups"]] == ["評審組"]
    # 更新（整筆替換）
    res = client.put(
        "/api/groups/評審組", json={"role_keys": ["pm", "qa", "senior"], "mode": "parallel"}
    )
    assert res.status_code == 200
    assert res.json()["group"]["mode"] == "parallel"
    g = client.get("/api/groups").json()["groups"][0]
    assert g["role_keys"] == ["pm", "qa", "senior"]
    # 刪除
    assert client.delete("/api/groups/評審組").status_code == 200
    assert client.get("/api/groups").json()["groups"] == []


def test_mode_defaults_to_round_robin_on_create(client):
    res = client.post("/api/groups", json={"name": "預設組", "role_keys": ["engineer", "senior"]})
    assert res.status_code == 200 and res.json()["group"]["mode"] == "round_robin"


def test_unknown_role_key_422(client):
    res = client.post(
        "/api/groups", json={"name": "g", "role_keys": ["engineer", "ghost"], "mode": "parallel"}
    )
    assert res.status_code == 422
    assert "ghost" in res.json()["error"] and "不存在" in res.json()["error"]


def test_duplicate_role_key_422(client):
    res = client.post(
        "/api/groups",
        json={"name": "g", "role_keys": ["engineer", "engineer"], "mode": "parallel"},
    )
    assert res.status_code == 422 and "不得重複" in res.json()["error"]


def test_single_member_422(client):
    res = client.post(
        "/api/groups", json={"name": "g", "role_keys": ["engineer"], "mode": "parallel"}
    )
    assert res.status_code == 422 and "≥2" in res.json()["error"]


def test_invalid_mode_422(client):
    for bad in ("legacy", "怪模式"):
        res = client.post(
            "/api/groups", json={"name": "g", "role_keys": ["engineer", "senior"], "mode": bad}
        )
        assert res.status_code == 422 and "mode" in res.json()["error"]
    # 422 全部被擋下，沒有任何小組落地
    assert client.get("/api/groups").json()["groups"] == []


def test_duplicate_name_409(client):
    body = {"name": "同名", "role_keys": ["engineer", "senior"], "mode": "parallel"}
    assert client.post("/api/groups", json=body).status_code == 200
    res = client.post("/api/groups", json=body)
    assert res.status_code == 409 and "已存在" in res.json()["error"]


def test_update_validates_and_404(client):
    client.post(
        "/api/groups", json={"name": "g", "role_keys": ["engineer", "senior"], "mode": "parallel"}
    )
    # 更新也走同套驗證（引用不存在角色被拒）
    res = client.put("/api/groups/g", json={"role_keys": ["engineer", "ghost"], "mode": "parallel"})
    assert res.status_code == 422 and "ghost" in res.json()["error"]
    # 不存在的小組 404
    res = client.put(
        "/api/groups/沒這組", json={"role_keys": ["engineer", "senior"], "mode": "parallel"}
    )
    assert res.status_code == 404


def test_delete_nonexistent_404(client):
    assert client.delete("/api/groups/沒這組").status_code == 404


def test_corrupt_groups_file_500(client, tmp_path):
    (config.ROLES_DIR / "groups.yaml").write_text("groups: [壞掉", encoding="utf-8")
    res = client.get("/api/groups")
    assert res.status_code == 500 and "YAML" in res.json()["error"]


def test_requires_auth_when_enabled(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    assert client.get("/api/groups").status_code == 401
    assert (
        client.post(
            "/api/groups", json={"name": "g", "role_keys": ["engineer", "senior"]}
        ).status_code
        == 401
    )


def test_writes_loopback_only_when_auth_disabled(tmp_path, monkeypatch):
    """門禁停用時，寫入端點須掛 WRITE_DEPS(require_admin) 退回僅限本機。

    非本機來源即使在門禁停用下也不得改 group（POST/PUT/DELETE 回 403）；GET 可讀。
    這條鎖住 `/api/groups` 寫入須與 `/api/roles` 同級保護，杜絕把控制面裸露給 0.0.0.0。
    """
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    from studio.server import app

    remote = TestClient(app, client=("203.0.113.7", 40000))  # 非本機
    payload = {"name": "g", "role_keys": ["engineer", "senior"], "mode": "round_robin"}
    assert remote.post("/api/groups", json=payload).status_code == 403
    assert remote.put("/api/groups/g", json={"role_keys": ["pm", "qa"]}).status_code == 403
    assert remote.delete("/api/groups/g").status_code == 403
    assert remote.get("/api/groups").status_code == 200  # 讀取不受限
