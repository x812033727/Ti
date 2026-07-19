"""快答內建流程(Kimi 化 PR13):保留名/取得/驗證/單專家一輪結構。"""

from __future__ import annotations

from studio import workflow


def test_quick_answer_builtin_registered():
    assert workflow.QUICK_ANSWER_NAME in workflow.RESERVED_NAMES
    wf = workflow.get_workflow(workflow.QUICK_ANSWER_NAME)
    assert wf and wf["name"] == "快答"


def test_quick_answer_structure_single_senior_one_round():
    wf = workflow.quick_answer_workflow()
    stages = wf["stages"]
    assert [s["type"] for s in stages] == ["discuss", "wrap_up"]
    d = stages[0]
    assert d["mode"] == "single" and d["roles"] == ["senior"] and d["max_rounds"] == 1


def test_quick_answer_passes_validation():
    # coerce=重新 validate 的單一入口:壞定義會 raise WorkflowError
    out = workflow.coerce(workflow.quick_answer_workflow())
    assert out["name"] == "快答"
