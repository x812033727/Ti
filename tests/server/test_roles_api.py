"""/api/roles CRUD 的 API 層測試（任務 #2）。

涵蓋驗收 #4（CRUD＋來源標記＋刪除語意）、#5（空殼 persona 防護）與
key 驗證（POST body 與 PUT/DELETE 路徑參數同套，防路徑穿越）、auth 門禁。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, role_store, roles

GOOD_PROMPT = (
    "你的角色：文件審查員。\n職責：檢查文件品質。\n最後一行輸出：`決議: 核可` 或 `決議: 退回`。"
)


def _payload(key: str, **over) -> dict:
    return {"key": key, "name": f"角色{key}", "system_prompt": GOOD_PROMPT, **over}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """門禁停用＋loopback peer（過 require_admin fail-safe）；ROLES_DIR 指向 tmp。

    測後清檔重載——保證離開時 roles 模組回到純內建狀態，不污染其他測試。
    """
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    roles_dir = tmp_path / "roles"
    monkeypatch.setattr(config, "ROLES_DIR", roles_dir)
    from studio.server import app

    yield TestClient(app, client=("127.0.0.1", 12345))
    if roles_dir.is_dir():
        for f in roles_dir.glob("*.md"):
            f.unlink()
    role_store.reload_roles()


# --- GET ---------------------------------------------------------------


def test_get_lists_builtin_roles_with_source(client):
    role_store.reload_roles()  # 確保乾淨起點
    data = client.get("/api/roles").json()
    by_key = {r["key"]: r for r in data["roles"]}
    assert set(by_key) == {r.key for r in roles.BUILTIN_ROLES}
    assert all(r["source"] == "builtin" for r in by_key.values())
    # system_prompt 回「角色專屬 body 原文」：非空、不含共通守則前綴
    eng = by_key["engineer"]
    assert eng["system_prompt"] and "AI 軟體工作室" not in eng["system_prompt"]
    assert eng["in_roster"] is True


# --- POST：建立 ----------------------------------------------------------


def test_post_creates_file_role(client):
    res = client.post("/api/roles", json=_payload("writer", description="寫文件"))
    assert res.status_code == 200
    role = res.json()["role"]
    assert role["source"] == "file" and role["name"] == "角色writer"
    # 檔案落地＋角色表 reload
    assert (config.ROLES_DIR / "writer.md").is_file()
    assert roles.BY_KEY["writer"].description == "寫文件"
    # 再 GET 可見
    listed = {r["key"]: r for r in client.get("/api/roles").json()["roles"]}
    assert listed["writer"]["source"] == "file"


def test_post_duplicate_key_conflicts(client):
    assert client.post("/api/roles", json=_payload("writer")).status_code == 200
    res = client.post("/api/roles", json=_payload("writer"))
    assert res.status_code == 409
    assert "已存在" in res.json()["detail"]


@pytest.mark.parametrize("bad", ["Bad-Key", "1abc", "a", "x" * 33, "../evil", "a/b"])
def test_post_invalid_key_rejected(client, bad):
    res = client.post("/api/roles", json=_payload(bad))
    assert res.status_code == 422
    assert "不合法" in res.json()["detail"]
    if config.ROLES_DIR.is_dir():  # 防路徑穿越：沒有任何檔被寫出
        assert list(config.ROLES_DIR.glob("**/*.md")) == []


def test_post_empty_prompt_rejected(client):
    res = client.post("/api/roles", json=_payload("hollow", system_prompt="  \n"))
    assert res.status_code == 422
    assert "不可為空" in res.json()["detail"]


def test_post_prompt_without_format_section_rejected(client):
    res = client.post("/api/roles", json=_payload("fluffy", system_prompt="你超棒、超聰明。"))
    assert res.status_code == 422
    assert "出力格式" in res.json()["detail"]
    assert "fluffy" not in roles.BY_KEY


def test_post_invalid_permission_mode_rejected(client):
    res = client.post("/api/roles", json=_payload("permy", permission_mode="bypassPermissions"))
    assert res.status_code == 422
    assert "permission_mode" in res.json()["detail"]


def test_post_builtin_key_creates_override(client):
    res = client.post("/api/roles", json=_payload("engineer", name="魔改工程師"))
    assert res.status_code == 200
    assert res.json()["role"]["source"] == "override"
    assert roles.BY_KEY["engineer"].name == "魔改工程師"
    assert roles.ENGINEER.name == "魔改工程師"  # 具名常數同步


def test_builtin_roundtrip_get_then_override(client):
    """守門：GET 讀出內建 body 原文，原樣 POST 成覆蓋檔，不被 persona 規則卡死。"""
    role_store.reload_roles()
    listed = {r["key"]: r for r in client.get("/api/roles").json()["roles"]}
    for key in ("engineer", "architect"):  # 出力標記最少的兩個內建
        res = client.post(
            "/api/roles",
            json=_payload(key, name=listed[key]["name"], system_prompt=listed[key]["system_prompt"]),
        )
        assert res.status_code == 200, res.json()
        assert res.json()["role"]["source"] == "override"


# --- PUT：編輯 -----------------------------------------------------------


def test_put_updates_file_role(client):
    client.post("/api/roles", json=_payload("writer"))
    res = client.put("/api/roles/writer", json=_payload("writer", name="新名字"))
    assert res.status_code == 200
    assert roles.BY_KEY["writer"].name == "新名字"


def test_put_builtin_creates_override(client):
    res = client.put("/api/roles/qa", json=_payload("qa", name="驗證魔人"))
    assert res.status_code == 200
    assert res.json()["role"]["source"] == "override"
    assert roles.BY_KEY["qa"].name == "驗證魔人"


def test_put_unknown_key_404(client):
    assert client.put("/api/roles/nobody", json=_payload("nobody")).status_code == 404


def test_put_key_mismatch_rejected(client):
    client.post("/api/roles", json=_payload("writer"))
    res = client.put("/api/roles/writer", json=_payload("other"))
    assert res.status_code == 422
    assert "不一致" in res.json()["detail"]


def test_put_path_key_validated(client):
    assert client.put("/api/roles/Bad-Key", json=_payload("")).status_code == 422


# --- DELETE：刪除／還原 ---------------------------------------------------


def test_delete_file_role_removes_it(client):
    client.post("/api/roles", json=_payload("writer"))
    res = client.delete("/api/roles/writer")
    assert res.status_code == 200
    assert res.json()["restored_builtin"] is False
    assert "writer" not in roles.BY_KEY
    assert not (config.ROLES_DIR / "writer.md").exists()


def test_delete_override_restores_builtin(client):
    client.post("/api/roles", json=_payload("engineer", name="魔改工程師"))
    assert roles.BY_KEY["engineer"].name == "魔改工程師"
    res = client.delete("/api/roles/engineer")
    assert res.status_code == 200
    assert res.json()["restored_builtin"] is True
    assert roles.BY_KEY["engineer"] is roles.BUILTIN_CORE[1]
    assert roles.ENGINEER is roles.BUILTIN_CORE[1]


def test_delete_pure_builtin_409(client):
    res = client.delete("/api/roles/pm")
    assert res.status_code == 409
    assert "內建角色" in res.json()["detail"]
    assert "pm" in roles.BY_KEY


def test_delete_unknown_404(client):
    assert client.delete("/api/roles/nobody").status_code == 404


def test_delete_path_key_validated(client):
    assert client.delete("/api/roles/Bad-Key").status_code == 422


# --- auth 門禁 ------------------------------------------------------------


def test_auth_enabled_blocks_anonymous(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    assert client.get("/api/roles").status_code == 401
    assert client.post("/api/roles", json=_payload("writer")).status_code == 401
    assert client.delete("/api/roles/writer").status_code == 401
