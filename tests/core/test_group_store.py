"""討論小組（role_store 的 Group 邏輯）離線單元測試。

涵蓋：三條硬規則（key 必須存在／不得重複／≥2 人）＋mode 白名單、CRUD 落檔
roundtrip、檔案不存在＝空清單、壞檔明確報錯、groups.yaml 不被角色載入器誤掃。
"""

from __future__ import annotations

import pytest
import yaml

from studio import config, role_store, roles


@pytest.fixture()
def roles_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    return tmp_path


# --- 驗證三硬規則＋mode 白名單 -----------------------------------------------


def test_validate_ok_normalizes(roles_dir):
    g = role_store.validate_group("  核心組 ", ["engineer ", "senior"], "round_robin")
    assert g == {"name": "核心組", "role_keys": ["engineer", "senior"], "mode": "round_robin"}


def test_unknown_role_key_rejected(roles_dir):
    with pytest.raises(role_store.GroupError, match="不存在的角色") as exc:
        role_store.validate_group("g", ["engineer", "ghost_role"], "parallel")
    assert "ghost_role" in str(exc.value)


def test_duplicate_role_key_rejected(roles_dir):
    with pytest.raises(role_store.GroupError, match="不得重複") as exc:
        role_store.validate_group("g", ["engineer", "engineer", "senior"], "parallel")
    assert "engineer" in str(exc.value)


def test_fewer_than_two_members_rejected(roles_dir):
    with pytest.raises(role_store.GroupError, match="≥2"):
        role_store.validate_group("g", ["engineer"], "round_robin")
    with pytest.raises(role_store.GroupError, match="≥2"):
        role_store.validate_group("g", [], "round_robin")


def test_invalid_mode_rejected(roles_dir):
    with pytest.raises(role_store.GroupError, match="mode"):
        role_store.validate_group("g", ["engineer", "senior"], "legacy")
    with pytest.raises(role_store.GroupError, match="mode"):
        role_store.validate_group("g", ["engineer", "senior"], "")


def test_empty_name_rejected(roles_dir):
    with pytest.raises(role_store.GroupError, match="name"):
        role_store.validate_group("   ", ["engineer", "senior"], "parallel")


def test_overlong_name_rejected(roles_dir):
    with pytest.raises(role_store.GroupError, match="過長"):
        role_store.validate_group("超" * 65, ["engineer", "senior"], "parallel")


def test_empty_string_key_rejected(roles_dir):
    with pytest.raises(role_store.GroupError, match="空字串"):
        role_store.validate_group("g", ["engineer", "  "], "parallel")


# --- CRUD 落檔 roundtrip ------------------------------------------------------


def test_missing_file_lists_empty(roles_dir):
    assert role_store.list_groups() == []
    assert role_store.get_group("沒有") is None


def test_create_get_update_delete_roundtrip(roles_dir):
    g = role_store.create_group("評審組", ["engineer", "senior"], "round_robin")
    assert g == {"name": "評審組", "role_keys": ["engineer", "senior"], "mode": "round_robin"}
    # 落檔可讀回（重新解析檔案，不是記憶體殘像）
    assert role_store.get_group("評審組") == g
    raw = yaml.safe_load((roles_dir / "groups.yaml").read_text(encoding="utf-8"))
    assert raw == {"groups": [g]}

    g2 = role_store.update_group("評審組", ["pm", "qa", "senior"], "parallel")
    assert g2["role_keys"] == ["pm", "qa", "senior"] and g2["mode"] == "parallel"
    assert role_store.get_group("評審組") == g2

    assert role_store.delete_group("評審組") is True
    assert role_store.list_groups() == []
    assert role_store.delete_group("評審組") is False


def test_create_duplicate_name_returns_none(roles_dir):
    assert role_store.create_group("g", ["engineer", "senior"], "parallel") is not None
    assert role_store.create_group("g", ["pm", "qa"], "round_robin") is None
    # 原小組未被覆蓋
    assert role_store.get_group("g")["role_keys"] == ["engineer", "senior"]


def test_update_nonexistent_returns_none(roles_dir):
    assert role_store.update_group("不存在", ["engineer", "senior"], "parallel") is None


def test_create_invalid_does_not_touch_file(roles_dir):
    with pytest.raises(role_store.GroupError):
        role_store.create_group("g", ["engineer"], "round_robin")
    assert not (roles_dir / "groups.yaml").exists()


# --- 壞檔防護 -----------------------------------------------------------------


def test_corrupt_yaml_raises_groupfileerror(roles_dir):
    (roles_dir / "groups.yaml").write_text("groups: [未閉合", encoding="utf-8")
    with pytest.raises(role_store.GroupFileError, match="YAML"):
        role_store.list_groups()


def test_wrong_structure_raises_groupfileerror(roles_dir):
    (roles_dir / "groups.yaml").write_text("- 不是映射\n", encoding="utf-8")
    with pytest.raises(role_store.GroupFileError, match="結構不符"):
        role_store.list_groups()
    (roles_dir / "groups.yaml").write_text("groups:\n  - name: g\n", encoding="utf-8")
    with pytest.raises(role_store.GroupFileError, match="第 1 筆"):
        role_store.list_groups()


def test_empty_file_lists_empty(roles_dir):
    (roles_dir / "groups.yaml").write_text("", encoding="utf-8")
    assert role_store.list_groups() == []


# --- 與角色載入器互不干擾 -------------------------------------------------------


def test_groups_yaml_not_loaded_as_role(roles_dir):
    """groups.yaml 放在 ROLES_DIR 內，但載入器只掃 *.md——不得被當角色檔誤拒。"""
    role_store.create_group("g", ["engineer", "senior"], "parallel")
    try:
        assert role_store.reload_roles() == {}
        assert "groups" not in roles.BY_KEY
    finally:
        (roles_dir / "groups.yaml").unlink()
        role_store.reload_roles()
