"""任務 #1 QA 獨立驗證：角色設定檔載入器（對齊驗收標準 #1/#2/#3）。

與工程師的 tests/core/test_role_store.py 互補，重點驗：
- 驗收 #1 向後相容：無角色檔時 ROSTER/BY_KEY 與純內建完全一致（含 BY_KEY ⊇ ROSTER 不對稱）。
- 驗收 #2 檔案覆蓋：同 key 覆蓋後 name/system_prompt 以檔案為準；新 key 出現在角色表。
- 驗收 #3 壞檔防護：缺必填／YAML 壞／未知欄位逐檔拒絕、log 可見原因、好檔與內建零受損。
- 模組級 import 綁定（orchestrator 的 `from .roles import ROSTER`）reload 後保活。
- 進行中 session 快照語意：reload 不改變已取出的 Role 物件。
"""

from __future__ import annotations

import logging

import pytest

from studio import config, role_store, roles
from studio.roles import BY_KEY as BOUND_BY_KEY  # 模擬 orchestrator 的模組級綁定
from studio.roles import ROSTER as BOUND_ROSTER

BODY = "職責：QA 驗證。\n最後一行輸出：`驗證: PASS` 或 `驗證: FAIL`。"


@pytest.fixture()
def rdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    yield tmp_path
    for f in tmp_path.glob("*.md"):
        f.unlink()
    role_store.reload_roles()


def _w(rdir, key, fm, body=BODY):
    (rdir / f"{key}.md").write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")


# --- 驗收 #1：向後相容 -------------------------------------------------------


def test_acc1_no_files_identical_to_builtin(rdir):
    assert role_store.reload_roles() == {}
    # ROSTER 與內建定義完全一致（鍵與物件皆同）
    expected_roster = list(roles.BUILTIN_CORE) + [
        r for r in roles.BUILTIN_OPTIONAL if r.key in config.OPTIONAL_ROLES
    ]
    assert roles.ROSTER == expected_roster
    assert all(a is b for a, b in zip(roles.ROSTER, expected_roster, strict=True))
    # BY_KEY ⊇ ROSTER 不對稱：含全部 8 內建（即使被 OPTIONAL_ROLES 過濾）
    assert set(roles.BY_KEY) == {r.key for r in roles.BUILTIN_ROLES}
    assert len(roles.BY_KEY) == 8


def test_acc1_by_key_superset_when_optional_filtered(rdir, monkeypatch):
    # 把 optional 全關掉：ROSTER 縮成 4 核心，但 BY_KEY 仍須含 8 內建
    monkeypatch.setattr(config, "OPTIONAL_ROLES", set())
    role_store.reload_roles()
    assert [r.key for r in roles.ROSTER] == ["pm", "engineer", "qa", "senior"]
    assert len(roles.BY_KEY) == 8  # improver 靠 `key not in BY_KEY` 判斷，不得縮水
    monkeypatch.undo()
    role_store.reload_roles()


# --- 驗收 #2：檔案覆蓋／新增 -------------------------------------------------


def test_acc2_override_name_and_prompt_from_file(rdir):
    _w(rdir, "qa", "name: 驗證大師\ntitle: 首席QA", "職責：抓蟲。\n最後一行輸出：`驗證: PASS`。")
    assert role_store.reload_roles() == {}
    r = roles.BY_KEY["qa"]
    assert r.name == "驗證大師" and r.title == "首席QA"
    assert "抓蟲" in r.system_prompt  # system_prompt 以檔案 body 為準
    assert r.system_prompt.startswith(roles._COMMON)  # 共通守則自動前置
    # 模組級綁定看得到同一份覆蓋（保活驗證）
    assert BOUND_BY_KEY["qa"] is r
    assert any(x is r for x in BOUND_ROSTER)


def test_acc2_new_key_appears_in_roster_and_by_key(rdir):
    _w(rdir, "writer", "name: 文案\ndescription: 寫文件")
    assert role_store.reload_roles() == {}
    assert "writer" in roles.BY_KEY
    assert any(r.key == "writer" for r in BOUND_ROSTER)
    assert role_store.role_source("writer") == "file"


def test_acc2_snapshot_semantics_existing_role_object_unchanged(rdir):
    """reload 只影響之後取用者：已快照的 Role 物件本身不被改動（frozen）。"""
    snapshot = roles.BY_KEY["engineer"]
    _w(rdir, "engineer", "name: 新工程師")
    role_store.reload_roles()
    assert snapshot.name != "新工程師"  # 舊物件不變
    assert roles.BY_KEY["engineer"].name == "新工程師"  # 新取用者拿到新的


# --- 驗收 #3：壞檔防護 -------------------------------------------------------


def test_acc3_mixed_bad_files_each_rejected_with_log(rdir, caplog):
    (rdir / "badyaml.md").write_text("---\nname: [oops\n---\n" + BODY, encoding="utf-8")
    _w(rdir, "nofield", "avatar: 🙃")  # 缺必填 name
    _w(rdir, "unknown", "name: 神秘\nmagic_power: 9000")  # 未知欄位
    _w(rdir, "ok", "name: 好人")

    with caplog.at_level(logging.WARNING, logger="ti.roles"):
        errors = role_store.reload_roles()

    # 三壞檔各自被拒、原因明確
    assert set(errors) == {"badyaml.md", "nofield.md", "unknown.md"}
    assert "name" in errors["nofield.md"]
    assert "magic_power" in errors["unknown.md"]  # 未知 key 不靜默忽略且指名
    # log 可見原因（每壞檔一筆 warning）
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) >= 3
    # 好檔與內建零受損
    assert "ok" in roles.BY_KEY
    for k in ("badyaml", "nofield", "unknown"):
        assert k not in roles.BY_KEY
    assert (
        len(
            [
                k
                for k in roles.BY_KEY
                if k
                in (
                    "pm",
                    "engineer",
                    "qa",
                    "senior",
                    "researcher",
                    "architect",
                    "security",
                    "devops",
                )
            ]
        )
        == 8
    )


def test_acc3_bad_override_keeps_builtin_intact(rdir):
    """覆蓋內建的檔案本身是壞檔 → 拒絕後該內建角色完整保留。"""
    _w(rdir, "pm", "name: 壞PM\nbogus_field: x")
    errors = role_store.reload_roles()
    assert "pm.md" in errors
    assert roles.BY_KEY["pm"] is roles.BUILTIN_CORE[0]  # 原封不動
    assert role_store.role_source("pm") == "builtin"


def test_acc3_empty_yaml_frontmatter_rejected(rdir):
    (rdir / "emptyfm.md").write_text("---\n\n---\n" + BODY, encoding="utf-8")
    errors = role_store.reload_roles()
    assert "emptyfm.md" in errors  # YAML 空映射 → 明確拒絕，非 crash


def test_acc3_reload_idempotent_after_errors(rdir):
    """壞檔在場時連續 reload 兩次，結果穩定（無累積污染）。"""
    _w(rdir, "dup", "name: 好\n")
    (rdir / "bad.md").write_text("not frontmatter", encoding="utf-8")
    e1 = role_store.reload_roles()
    snap = dict(roles.BY_KEY)
    e2 = role_store.reload_roles()
    assert e1 == e2 and "bad.md" in e2
    assert set(roles.BY_KEY) == set(snap)
