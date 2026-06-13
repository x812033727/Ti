"""任務 #3 QA 獨立驗證：討論小組（Group）CRUD 與三條硬規則。

驗收標準 #6：建立小組引用不存在 key／重複 key／僅 1 人／非法 mode 各回 4xx
明確訊息；合法小組可建立、GET 可查、可更新刪除。
另補：落檔位置（<ROLES_DIR>/groups.yaml）、檔案內容可回讀、PUT 也須過同套驗證、
空 name、0 人、role_keys 含空字串、檔案角色入隊等邊界。
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from studio import config, role_store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    from studio.server import app

    # 寫入端點掛 WRITE_DEPS(require_admin)：門禁停用時退回僅限本機，故用 loopback peer 放行。
    return TestClient(app, client=("127.0.0.1", 23456))


# --- 驗收 #6：四種 4xx，逐一可歸因 ---------------------------------------


def test_qa_nonexistent_role_key_rejected(client):
    res = client.post(
        "/api/groups",
        json={"name": "幽靈組", "role_keys": ["engineer", "no_such_role"], "mode": "parallel"},
    )
    assert res.status_code == 422
    # 訊息須點名不存在的 key（自證對應，而非模糊的「驗證失敗」）
    assert "no_such_role" in res.json()["error"]
    # 失敗不得落檔
    assert client.get("/api/groups").json()["groups"] == []


def test_qa_duplicate_keys_rejected(client):
    res = client.post(
        "/api/groups",
        json={"name": "雙胞胎", "role_keys": ["qa", "qa", "engineer"], "mode": "round_robin"},
    )
    assert res.status_code == 422
    assert "qa" in res.json()["error"]


def test_qa_single_member_rejected(client):
    res = client.post(
        "/api/groups", json={"name": "獨行俠", "role_keys": ["engineer"], "mode": "round_robin"}
    )
    assert res.status_code == 422
    assert "2" in res.json()["error"]


def test_qa_zero_member_rejected(client):
    res = client.post("/api/groups", json={"name": "空組", "role_keys": [], "mode": "round_robin"})
    assert res.status_code == 422


def test_qa_illegal_mode_rejected(client):
    for bad in ("legacy", "roundrobin", "", "PARALLEL"):
        res = client.post(
            "/api/groups",
            json={"name": f"壞模式{bad}", "role_keys": ["pm", "qa"], "mode": bad},
        )
        assert res.status_code == 422, f"mode={bad!r} 應 422，得 {res.status_code}"
        assert "mode" in res.json()["error"]


def test_qa_blank_name_and_empty_key_item_rejected(client):
    assert (
        client.post(
            "/api/groups", json={"name": "  ", "role_keys": ["pm", "qa"], "mode": "parallel"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/groups", json={"name": "空項", "role_keys": ["pm", " "], "mode": "parallel"}
        ).status_code
        == 422
    )


# --- 驗收 #6：happy path＋落檔自證 ----------------------------------------


def test_qa_full_crud_and_file_persistence(client, tmp_path):
    # 建立兩組
    for name, keys, mode in (
        ("審查組", ["senior", "qa"], "round_robin"),
        ("攻防組", ["security", "engineer", "architect"], "parallel"),
    ):
        res = client.post("/api/groups", json={"name": name, "role_keys": keys, "mode": mode})
        assert res.status_code == 200, res.text

    # 落檔位置與內容自證（直接讀 groups.yaml，不經 API）
    gfile = tmp_path / "groups.yaml"
    assert gfile.is_file(), "groups.yaml 須落在 ROLES_DIR"
    data = yaml.safe_load(gfile.read_text(encoding="utf-8"))
    assert [g["name"] for g in data["groups"]] == ["審查組", "攻防組"]

    # GET 可查
    groups = client.get("/api/groups").json()["groups"]
    assert len(groups) == 2
    assert groups[1]["role_keys"] == ["security", "engineer", "architect"]

    # 同名 409
    assert (
        client.post(
            "/api/groups", json={"name": "審查組", "role_keys": ["pm", "qa"], "mode": "parallel"}
        ).status_code
        == 409
    )

    # PUT 整筆替換生效，且 PUT 同套驗證（壞 mode / 不存在 key 也 422）
    res = client.put("/api/groups/審查組", json={"role_keys": ["pm", "qa"], "mode": "parallel"})
    assert res.status_code == 200
    assert res.json()["group"] == {"name": "審查組", "role_keys": ["pm", "qa"], "mode": "parallel"}
    assert (
        client.put(
            "/api/groups/審查組", json={"role_keys": ["pm", "qa"], "mode": "bad"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/groups/審查組", json={"role_keys": ["pm", "ghost"], "mode": "parallel"}
        ).status_code
        == 422
    )
    # PUT 不存在的小組 404
    assert (
        client.put(
            "/api/groups/不存在", json={"role_keys": ["pm", "qa"], "mode": "parallel"}
        ).status_code
        == 404
    )

    # DELETE 生效；再刪 404
    assert client.delete("/api/groups/攻防組").status_code == 200
    assert client.delete("/api/groups/攻防組").status_code == 404
    assert [g["name"] for g in client.get("/api/groups").json()["groups"]] == ["審查組"]
    # 檔案同步縮減
    data = yaml.safe_load(gfile.read_text(encoding="utf-8"))
    assert len(data["groups"]) == 1


def test_qa_file_role_can_join_group(client, tmp_path):
    """新 key 檔案角色 reload 後可入隊（key 存在性看 BY_KEY，含檔案角色）。"""
    (tmp_path / "lawyer.md").write_text(
        "---\nname: 法務\n---\n審閱合約。\n輸出: 每次給結論一句。\n", encoding="utf-8"
    )
    errors = role_store.reload_roles()
    assert errors == {}
    try:
        res = client.post(
            "/api/groups",
            json={"name": "法務組", "role_keys": ["lawyer", "pm"], "mode": "parallel"},
        )
        assert res.status_code == 200, res.text
    finally:
        (tmp_path / "lawyer.md").unlink()
        role_store.reload_roles()  # 還原內建，避免污染其他測試


def test_qa_corrupt_groups_yaml_returns_500_not_crash(client, tmp_path):
    (tmp_path / "groups.yaml").write_text("groups: [unclosed", encoding="utf-8")
    res = client.get("/api/groups")
    assert res.status_code == 500
    assert "groups.yaml" in res.json()["error"]
