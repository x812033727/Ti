"""QA 守護：閘門層級標籤的「完整性契約」——防新閘門漏標。

`test_qa_task3b_gate_level_labels` 已逐條驗證現有三閘門（lint/collect/test）的 return
值與 `_handle_gate_failure` 的 backlog note 都帶 `[label]` 前綴。但它對「閘門集合」只用
寬鬆檢查（`labels[:3] == [...]` 加 `"merge" in labels`）——未來若有人新增第 N 個帶標閘門，
test3b 仍會通過，等於放任閘門清單悄悄漂移、也放過「新閘門忘了走帶標 handler」的情形。

本檔把閘門 → label 的對應鎖成**精確、有序的契約**：任何新增／移除／改名閘門，都必須
有意識地同步更新此清單，否則直接紅。如此「防漏標」不靠脆弱的白名單私改，而靠單一真值
來源的精確比對。
"""

from __future__ import annotations

import ast
import inspect

from studio import autopilot

# run_one_task 內，依序通過後才能合併進 main 的客觀閘門。改動閘門集合時須一併更新。
EXPECTED_GATE_LABELS = ["lint", "collect", "test", "merge"]


def _routed_gate_labels() -> list[str]:
    """解析 `run_one_task` 原始碼，依出現順序取出所有
    `_handle_gate_failure(task, "<label>", ...)` 的字面 label。

    用 AST（非硬編副本）確保契約跟著真實程式碼走；依 lineno 排序保證結果決定性，
    不依賴 `ast.walk` 的遍歷順序。
    """
    src = inspect.getsource(autopilot.run_one_task)
    tree = ast.parse(src)
    calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_handle_gate_failure"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            calls.append((node.lineno, node.args[1].value))
    return [label for _, label in sorted(calls)]


def test_gate_label_sequence_is_exact_contract():
    """閘門 label 序列須與契約逐一相符——任何漂移強制有意識地更新本測試。"""
    labels = _routed_gate_labels()
    assert labels == EXPECTED_GATE_LABELS, (
        "閘門 label 序列已漂移；新增/移除/改名閘門時，請同步更新 EXPECTED_GATE_LABELS "
        f"並確認 backlog note 仍帶 [label] 前綴。實得：{labels}"
    )


def test_routed_labels_are_clean_bracketable_tokens():
    """每個 label 都是非空、可被 `[{label}]` 乾淨包裹的單一識別字。

    `_handle_gate_failure` 用 `f"[{gate_label}] ..."` 組 note；label 若含空白或方括號，
    一眼辨層的語意就會破。鎖死 label 形態，杜絕 `[lint ]`／`[[test]]` 之類髒標籤。
    """
    labels = _routed_gate_labels()
    assert labels, "run_one_task 內找不到任何帶標 _handle_gate_failure 呼叫，測試前提已變"
    for label in labels:
        assert label and label.strip() == label, f"label 不應含前後空白：{label!r}"
        assert " " not in label, f"label 應為單一識別字、不含空白：{label!r}"
        assert "[" not in label and "]" not in label, f"label 不應自帶方括號：{label!r}"
