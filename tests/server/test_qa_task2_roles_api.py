"""任務 #2 QA 驗證：/api/roles CRUD 的獨立測試（不重複工程師既有案，補邊界）。

對應驗收：
- #4 API CRUD 可用（落檔、reload、來源標記、刪除語意全鏈）
- #5 空殼 persona 防護（4xx＋說明缺什麼）
- 設計決策：PUT 整筆替換語意、key 邊界、原子寫不留殘檔、檔案可被載入器 round-trip
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, role_store, roles

PROMPT = "你的角色：測試假人。\n職責：示範。\n最後一行輸出：`決議: 核可`。"


def _body(key: str, **over) -> dict:
    return {"key": key, "name": f"n_{key}", "system_prompt": PROMPT, **over}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path / "roles")
    from studio.server import app

    yield TestClient(app, client=("127.0.0.1", 23456))
    for f in (tmp_path / "roles").glob("*.md") if (tmp_path / "roles").is_dir() else []:
        f.unlink()
    role_store.reload_roles()


# --- 全鏈：建→列→改→刪（驗收 #4 一條龍）---------------------------------


def test_full_crud_chain(client):
    role_store.reload_roles()
    n0 = len(client.get("/api/roles").json()["roles"])

    # 建立
    r = client.post("/api/roles", json=_body("scribe", tags=["docs"], avatar="📝"))
    assert r.status_code == 200, r.json()
    assert r.json()["role"]["source"] == "file"
    path = config.ROLES_DIR / "scribe.md"
    assert path.is_file()

    # 落地檔可被載入器獨立解析（API 寫出＝載入器讀回，round-trip）
    parsed = role_store.parse_role_file(path)
    assert parsed.name == "n_scribe" and parsed.tags == ["docs"]
    assert parsed.system_prompt.startswith(roles._COMMON)  # _COMMON 自動前置

    # 列出 +1
    listed = client.get("/api/roles").json()["roles"]
    assert len(listed) == n0 + 1

    # 編輯生效
    r = client.put("/api/roles/scribe", json=_body("scribe", name="改名"))
    assert r.status_code == 200 and roles.BY_KEY["scribe"].name == "改名"

    # 刪除＝移除
    r = client.delete("/api/roles/scribe")
    assert r.status_code == 200
    assert "scribe" not in roles.BY_KEY and not path.exists()
    assert len(client.get("/api/roles").json()["roles"]) == n0


def test_override_then_restore_roundtrip(client):
    """內建覆蓋→GET 標記 override→DELETE 還原 builtin，且 ROSTER 內物件同步。"""
    role_store.reload_roles()
    orig = roles.BY_KEY["qa"]
    assert client.post("/api/roles", json=_body("qa", name="QA改")).status_code == 200
    listed = {x["key"]: x for x in client.get("/api/roles").json()["roles"]}
    assert listed["qa"]["source"] == "override" and listed["qa"]["name"] == "QA改"
    assert any(r.key == "qa" and r.name == "QA改" for r in roles.ROSTER)  # ROSTER 同步
    assert client.delete("/api/roles/qa").json()["restored_builtin"] is True
    assert roles.BY_KEY["qa"] is orig
    assert {x["key"]: x for x in client.get("/api/roles").json()["roles"]}["qa"][
        "source"
    ] == "builtin"


# --- PUT 整筆替換語意 ------------------------------------------------------


def test_put_full_replace_resets_omitted_fields(client):
    client.post("/api/roles", json=_body("scribe", tags=["docs"], title="文書", avatar="📝"))
    client.put("/api/roles/scribe", json=_body("scribe"))  # 未帶選填欄位
    role = roles.BY_KEY["scribe"]
    assert role.tags == [] and role.avatar == "🤖"  # 回預設，非殘留舊值


# --- 空殼 persona（驗收 #5：訊息要說明缺什麼）-----------------------------


@pytest.mark.parametrize(
    "prompt,frag",
    [
        ("", "不可為空"),
        ("   \n\t", "不可為空"),
        ("你是嚴謹又聰明的專家，輸出很棒。", "出力格式"),  # 有「輸出」但無冒號
        ("格式很重要。決議也很重要。", "出力格式"),  # 關鍵詞無冒號
    ],
)
def test_hollow_persona_rejected_with_reason(client, prompt, frag):
    r = client.post("/api/roles", json=_body("hollow", system_prompt=prompt))
    assert r.status_code == 422
    assert frag in r.json()["detail"]
    assert not (config.ROLES_DIR / "hollow.md").exists()  # 驗證失敗不落檔


def test_fullwidth_colon_accepted(client):
    r = client.post(
        "/api/roles", json=_body("fw", system_prompt="職責：示範。\n最後輸出：`決議：過`")
    )
    assert r.status_code == 200


def test_put_hollow_persona_rejected_and_old_file_kept(client):
    client.post("/api/roles", json=_body("scribe"))
    r = client.put("/api/roles/scribe", json=_body("scribe", system_prompt="空殼"))
    assert r.status_code == 422
    assert roles.BY_KEY["scribe"].name == "n_scribe"  # 舊資料不受影響
    assert (config.ROLES_DIR / "scribe.md").is_file()


# --- key 邊界與原子寫 -------------------------------------------------------


def test_key_length_boundaries(client):
    assert client.post("/api/roles", json=_body("ab")).status_code == 200  # 2 字下界
    assert client.post("/api/roles", json=_body("a" * 32)).status_code == 200  # 32 上界
    assert client.post("/api/roles", json=_body("a" * 33)).status_code == 422
    assert client.post("/api/roles", json=_body("a_9z")).status_code == 200


def test_no_tmp_residue_after_writes(client):
    client.post("/api/roles", json=_body("scribe"))
    client.put("/api/roles/scribe", json=_body("scribe", name="x"))
    assert list(config.ROLES_DIR.glob("*.tmp")) == []
    assert list(config.ROLES_DIR.glob(".*.tmp")) == []


def test_builtin_eight_personas_pass_micro_rules(client):
    """守門：內建 8 角色 body 全過 persona 規則（override 往返不卡死）。"""
    role_store.reload_roles()
    for r in roles.BUILTIN_ROLES:
        role_store.validate_persona_body(role_store.builtin_body(r))  # 不應 raise
