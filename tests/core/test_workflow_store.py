"""動態流程（studio.workflow）schema 驗證與檔案 store 離線單元測試。

涵蓋：default_workflow 自洽（驗證通過＋序列化往返）、schema 硬規則（未知欄位／型別／
角色存在性／verdict 白名單／mode／build 必含 task_pipeline／when 格式／層級限制）、
CRUD 落檔 roundtrip、預設名解析、檔案不存在＝空、壞檔明確報錯、workflows.yaml 不被
角色載入器誤掃。
"""

from __future__ import annotations

import pytest
import yaml

from studio import config, role_store, roles, workflow


@pytest.fixture()
def roles_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLES_DIR", tmp_path)
    return tmp_path


# --- default_workflow 自洽 ----------------------------------------------------


def test_default_workflow_validates():
    wf = workflow.default_workflow()
    # 重新 validate 不應拋錯，且正規化後與原本等價（單一真相穩定）。
    revalidated = workflow.validate_workflow(wf["name"], wf["description"], wf["stages"])
    assert revalidated == wf


def test_default_workflow_yaml_roundtrip():
    wf = workflow.default_workflow()
    restored = yaml.safe_load(yaml.safe_dump(wf, allow_unicode=True, sort_keys=False))
    assert (
        workflow.validate_workflow(restored["name"], restored["description"], restored["stages"])
        == wf
    )


def test_default_workflow_shape():
    wf = workflow.default_workflow()
    types = [s["type"] for s in wf["stages"]]
    assert types == [
        "clarify",
        "research",
        "decompose",
        "discuss",
        "build",
        "integrate",
        "demo",
        "wrap_up",
        "publish",
    ]
    build = next(s for s in wf["stages"] if s["type"] == "build")
    assert [s["type"] for s in build["task_pipeline"]] == ["implement", "review", "gate"]


def test_coerce_none_is_default():
    assert workflow.coerce(None) == workflow.default_workflow()


def test_coerce_invalid_falls_back_to_default():
    bad = {"name": "壞", "stages": [{"type": "nonsense"}]}
    assert workflow.coerce(bad) == workflow.default_workflow()


# --- schema 硬規則 ------------------------------------------------------------


def test_unknown_field_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="欄位驗證失敗"):
        workflow.validate_workflow("w", "", [{"type": "decompose", "bogus": 1}])


def test_unknown_stage_type_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="不合法"):
        workflow.validate_workflow("w", "", [{"type": "teleport"}])


def test_task_stage_type_rejected_at_session_level(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="不合法"):
        workflow.validate_workflow("w", "", [{"type": "implement"}])


def test_unknown_role_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="不存在的角色"):
        workflow.validate_workflow("w", "", [{"type": "discuss", "roles": ["ghost"]}])


def test_unknown_assignee_rejected(roles_dir):
    build = {
        "type": "build",
        "task_pipeline": [{"type": "implement", "assignee": "ghost"}],
    }
    with pytest.raises(workflow.WorkflowError, match="不存在的角色"):
        workflow.validate_workflow("w", "", [build])


def test_bad_verdict_rejected(roles_dir):
    build = {
        "type": "build",
        "task_pipeline": [{"type": "review", "gate": [{"role": "qa", "verdict": "not_a_verdict"}]}],
    }
    with pytest.raises(workflow.WorkflowError, match="白名單"):
        workflow.validate_workflow("w", "", [build])


def test_bad_mode_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="mode"):
        workflow.validate_workflow("w", "", [{"type": "discuss", "mode": "telepathy"}])


def test_build_requires_task_pipeline(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="task_pipeline"):
        workflow.validate_workflow("w", "", [{"type": "build"}])


def test_non_build_cannot_have_task_pipeline(roles_dir):
    bad = {"type": "demo", "task_pipeline": [{"type": "implement"}]}
    with pytest.raises(workflow.WorkflowError, match="task_pipeline"):
        workflow.validate_workflow("w", "", [bad])


def test_when_format_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="when"):
        workflow.validate_workflow("w", "", [{"type": "research", "when": "maybe"}])


def test_when_format_accepted(roles_dir):
    wf = workflow.validate_workflow(
        "w", "", [{"type": "research", "when": "has:researcher", "optional": True}]
    )
    assert wf["stages"][0]["when"] == "has:researcher"
    wf2 = workflow.validate_workflow(
        "w",
        "",
        [
            {
                "type": "build",
                "when": "flag:PARALLEL_TASKS_ENABLED",
                "task_pipeline": [{"type": "implement"}],
            }
        ],
    )
    assert wf2["stages"][0]["when"] == "flag:PARALLEL_TASKS_ENABLED"


def test_negative_budget_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="budget"):
        workflow.validate_workflow("w", "", [{"type": "dynamic", "budget": -1}])


def test_dynamic_bad_fallback_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="不存在的角色"):
        workflow.validate_workflow("w", "", [{"type": "dynamic", "fallback": "ghost"}])


def test_empty_stages_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="stage"):
        workflow.validate_workflow("w", "", [])


def test_empty_name_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="name"):
        workflow.validate_workflow("  ", "", [{"type": "demo"}])


def test_overlong_name_rejected(roles_dir):
    with pytest.raises(workflow.WorkflowError, match="過長"):
        workflow.validate_workflow("超" * 65, "", [{"type": "demo"}])


# --- CRUD 落檔 roundtrip ------------------------------------------------------


def test_missing_file_lists_empty(roles_dir):
    assert workflow.list_workflows() == []
    assert workflow.get_workflow("沒有") is None


def test_get_default_name_returns_builtin(roles_dir):
    # 沒有同名檔案時，預設名回內建 default_workflow()。
    assert workflow.get_workflow(workflow.DEFAULT_WORKFLOW_NAME) == workflow.default_workflow()


def test_create_get_update_delete_roundtrip(roles_dir):
    stages = [
        {"type": "decompose"},
        {"type": "build", "task_pipeline": [{"type": "implement", "assignee": "engineer"}]},
        {"type": "demo"},
    ]
    wf = workflow.create_workflow("快速原型", "精簡", stages)
    assert wf is not None and wf["name"] == "快速原型"
    # 落檔可讀回（重新解析檔案，不是記憶體殘像）。
    assert workflow.get_workflow("快速原型") == wf
    raw = yaml.safe_load((roles_dir / "workflows.yaml").read_text(encoding="utf-8"))
    assert raw == {"workflows": [wf]}

    wf2 = workflow.update_workflow("快速原型", "改版", [{"type": "demo"}])
    assert wf2["description"] == "改版" and [s["type"] for s in wf2["stages"]] == ["demo"]
    assert workflow.get_workflow("快速原型") == wf2

    assert workflow.delete_workflow("快速原型") is True
    assert workflow.list_workflows() == []
    assert workflow.delete_workflow("快速原型") is False


def test_create_duplicate_name_returns_none(roles_dir):
    assert workflow.create_workflow("w", "", [{"type": "demo"}]) is not None
    assert workflow.create_workflow("w", "", [{"type": "wrap_up"}]) is None
    assert [s["type"] for s in workflow.get_workflow("w")["stages"]] == ["demo"]


def test_cannot_create_reserved_default_name(roles_dir):
    # 預設名為保留字，不可被檔案覆蓋（避免遮蔽內建單一真相）。
    assert workflow.create_workflow(workflow.DEFAULT_WORKFLOW_NAME, "", [{"type": "demo"}]) is None


def test_update_nonexistent_returns_none(roles_dir):
    assert workflow.update_workflow("不存在", "", [{"type": "demo"}]) is None


def test_create_invalid_does_not_touch_file(roles_dir):
    with pytest.raises(workflow.WorkflowError):
        workflow.create_workflow("w", "", [{"type": "build"}])  # 缺 task_pipeline
    assert not (roles_dir / "workflows.yaml").exists()


# --- 壞檔防護 -----------------------------------------------------------------


def test_corrupt_yaml_raises_fileerror(roles_dir):
    (roles_dir / "workflows.yaml").write_text("workflows: [未閉合", encoding="utf-8")
    with pytest.raises(workflow.WorkflowFileError, match="YAML"):
        workflow.list_workflows()


def test_wrong_structure_raises_fileerror(roles_dir):
    (roles_dir / "workflows.yaml").write_text("- 不是映射\n", encoding="utf-8")
    with pytest.raises(workflow.WorkflowFileError, match="結構不符"):
        workflow.list_workflows()
    (roles_dir / "workflows.yaml").write_text("workflows:\n  - name: w\n", encoding="utf-8")
    with pytest.raises(workflow.WorkflowFileError, match="第 1 筆"):
        workflow.list_workflows()


def test_empty_file_lists_empty(roles_dir):
    (roles_dir / "workflows.yaml").write_text("", encoding="utf-8")
    assert workflow.list_workflows() == []


# --- 與角色載入器互不干擾 -------------------------------------------------------


def test_workflows_yaml_not_loaded_as_role(roles_dir):
    """workflows.yaml 放在 ROLES_DIR 內，但角色載入器只掃 *.md——不得被當角色檔誤拒。"""
    workflow.create_workflow("w", "", [{"type": "demo"}])
    try:
        assert role_store.reload_roles() == {}
        assert "workflows" not in roles.BY_KEY
    finally:
        (roles_dir / "workflows.yaml").unlink()
        role_store.reload_roles()
