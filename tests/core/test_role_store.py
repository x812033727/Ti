"""role_store（角色設定檔載入器）的離線單元測試。

涵蓋：檔案覆蓋內建／新 key 追加／壞檔被拒不影響其他角色／未知欄位明確報錯／
反空殼 persona 驗證／內建 8 角色 body 守門／刪檔還原內建／無檔案時行為與現狀一致。
"""

from __future__ import annotations

import logging

import pytest

from studio import config, role_store, roles


def _write_role(roles_dir, key, frontmatter, body):
    (roles_dir / f"{key}.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


GOOD_BODY = "你的角色：審查助手。\n職責：檢查文件。\n最後一行輸出：`決議: 核可` 或 `決議: 退回`。"


@pytest.fixture()
def roles_dir(tmp_path, monkeypatch):
    """把 ROLES_DIR 指向 tmp，測後清檔重載——保證離開時 roles 模組回到純內建狀態。"""
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    yield tmp_path
    for f in tmp_path.glob("*.md"):
        f.unlink()
    role_store.reload_roles()


# --- 向後相容：無角色檔 ---------------------------------------------------


def test_no_files_keeps_builtin_behavior(roles_dir):
    roster_before = list(roles.ROSTER)
    by_key_before = dict(roles.BY_KEY)
    roster_obj, by_key_obj = roles.ROSTER, roles.BY_KEY

    errors = role_store.reload_roles()

    assert errors == {}
    assert roles.ROSTER == roster_before
    assert roles.BY_KEY == by_key_before
    # 原地變異：模組級綁定（orchestrator 的 `from .roles import ROSTER`）必須保活。
    assert roles.ROSTER is roster_obj and roles.BY_KEY is by_key_obj
    assert [r.key for r in roles.CORE_ROLES] == ["pm", "engineer", "qa", "senior"]


def test_missing_dir_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path / "nonexistent")
    before = dict(roles.BY_KEY)
    assert role_store.reload_roles() == {}
    assert roles.BY_KEY == before


# --- 檔案覆蓋內建 -----------------------------------------------------------


def test_file_overrides_builtin(roles_dir):
    _write_role(roles_dir, "engineer", 'name: 魔改工程師\navatar: "🦾"', GOOD_BODY)

    assert role_store.reload_roles() == {}
    r = roles.BY_KEY["engineer"]
    assert r.name == "魔改工程師" and r.avatar == "🦾"
    # body 自動前置 _COMMON 共通守則
    assert r.system_prompt.startswith(roles._COMMON)
    assert "審查助手" in r.system_prompt
    # 具名常數同步（improver/autopilot 函式內 import 用）
    assert roles.ENGINEER is r
    # ROSTER 內同位置被換掉、CORE_ROLES 同步
    assert roles.ROSTER[1] is r and roles.CORE_ROLES[1] is r
    assert role_store.role_source("engineer") == "override"


def test_delete_override_restores_builtin(roles_dir):
    _write_role(roles_dir, "engineer", "name: 魔改工程師", GOOD_BODY)
    role_store.reload_roles()
    assert roles.BY_KEY["engineer"].name == "魔改工程師"

    (roles_dir / "engineer.md").unlink()
    role_store.reload_roles()
    assert roles.BY_KEY["engineer"] is roles.BUILTIN_CORE[1]
    assert roles.ENGINEER is roles.BUILTIN_CORE[1]
    assert role_store.role_source("engineer") == "builtin"


# --- 新 key 追加 -------------------------------------------------------------


def test_new_key_role_added_with_defaults(roles_dir):
    _write_role(roles_dir, "reviewer", "name: 文件審查員\ndescription: 專看文件", GOOD_BODY)

    assert role_store.reload_roles() == {}
    r = roles.BY_KEY["reviewer"]
    assert r.name == "文件審查員" and r.description == "專看文件"
    # 選填欄位預設值
    assert r.model == config.MODEL_FAST
    assert r.allowed_tools == ["Read", "Grep"]
    assert r.permission_mode == "default"
    assert r.title == "reviewer"
    # 出現在 ROSTER 尾端（內建之後）
    assert roles.ROSTER[-1] is r
    assert role_store.role_source("reviewer") == "file"
    # 內建角色不受影響
    assert roles.BY_KEY["pm"] is roles.BUILTIN_CORE[0]


# --- 壞檔防護 ---------------------------------------------------------------


def test_bad_yaml_rejected_others_unaffected(roles_dir, caplog):
    (roles_dir / "broken.md").write_text("---\nname: [未閉合\n---\nbody", encoding="utf-8")
    _write_role(roles_dir, "goodone", "name: 好角色", GOOD_BODY)

    with caplog.at_level(logging.WARNING, logger="ti.roles"):
        errors = role_store.reload_roles()

    assert "broken.md" in errors and "YAML" in errors["broken.md"]
    assert any("broken" in rec.message or "broken" in str(rec.args) for rec in caplog.records)
    # 好檔照常載入、內建完整
    assert "goodone" in roles.BY_KEY
    assert "broken" not in roles.BY_KEY
    assert all(r.key in roles.BY_KEY for r in roles.BUILTIN_ROLES)


def test_missing_required_field_rejected(roles_dir):
    _write_role(roles_dir, "noname", "avatar: 🤖", GOOD_BODY)
    errors = role_store.reload_roles()
    assert "noname.md" in errors and "name" in errors["noname.md"]
    assert "noname" not in roles.BY_KEY


def test_unknown_field_rejected_explicitly(roles_dir):
    _write_role(roles_dir, "extra", "name: 多欄位\nsuperpower: 飛行", GOOD_BODY)
    errors = role_store.reload_roles()
    # 未知 key 不得靜默忽略：明確報錯且指名欄位
    assert "extra.md" in errors and "superpower" in errors["extra.md"]
    assert "extra" not in roles.BY_KEY


def test_key_mismatch_rejected(roles_dir):
    _write_role(roles_dir, "alpha", "key: beta\nname: 不一致", GOOD_BODY)
    errors = role_store.reload_roles()
    assert "alpha.md" in errors and "不一致" in errors["alpha.md"]
    assert "alpha" not in roles.BY_KEY and "beta" not in roles.BY_KEY


def test_invalid_filename_key_rejected(roles_dir):
    (roles_dir / "Bad-Key.md").write_text(f"---\nname: 壞檔名\n---\n{GOOD_BODY}", encoding="utf-8")
    errors = role_store.reload_roles()
    assert "Bad-Key.md" in errors


def test_invalid_permission_mode_rejected(roles_dir):
    _write_role(roles_dir, "permy", "name: 權限怪\npermission_mode: bypassPermissions", GOOD_BODY)
    errors = role_store.reload_roles()
    assert "permy.md" in errors and "permission_mode" in errors["permy.md"]


def test_missing_frontmatter_rejected(roles_dir):
    (roles_dir / "nofm.md").write_text("沒有 frontmatter 的純文字", encoding="utf-8")
    errors = role_store.reload_roles()
    assert "nofm.md" in errors and "frontmatter" in errors["nofm.md"]


def test_sample_extension_not_loaded(roles_dir):
    (roles_dir / "_example.md.sample").write_text("---\nname: 範例\n---\nbody", encoding="utf-8")
    assert role_store.reload_roles() == {}


# --- 反空殼 persona ----------------------------------------------------------


def test_empty_body_rejected(roles_dir):
    _write_role(roles_dir, "empty", "name: 空殼", "   \n")
    errors = role_store.reload_roles()
    assert "empty.md" in errors and "不可為空" in errors["empty.md"]


def test_body_without_format_section_rejected(roles_dir):
    _write_role(roles_dir, "fluffy", "name: 形容詞俠", "你超棒、超聰明、超有經驗。")
    errors = role_store.reload_roles()
    assert "fluffy.md" in errors and "出力格式" in errors["fluffy.md"]


def test_all_builtin_bodies_pass_persona_rule():
    """守門：內建 8 角色的專屬 body 全數通過反空殼驗證。

    防未來改內建 prompt 時，override 的「讀出→改→寫回」往返被本規則卡死。
    """
    for r in roles.BUILTIN_ROLES:
        body = role_store.builtin_body(r)
        assert body != r.system_prompt, f"{r.key} 的 _COMMON 前綴剝除失敗"
        role_store.validate_persona_body(body)  # 不應 raise


# --- 環境變數 ---------------------------------------------------------------


def test_roles_dir_env_respected_via_reload(monkeypatch, tmp_path):
    monkeypatch.setenv("TI_ROLES_DIR", str(tmp_path / "custom"))
    config.reload()
    try:
        assert str(config.ROLES_DIR) == str(tmp_path / "custom")
    finally:
        monkeypatch.delenv("TI_ROLES_DIR")
        config.reload()
