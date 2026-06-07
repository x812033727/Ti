"""QA 獨立驗證（任務 #10 / 驗收標準 1）：workspace.py 只有『一份』containment 邏輯。

用 AST 靜態分析釘死架構不變式（純字串比對易誤判，故走 AST）：
- containment 的兩個關鍵運算（resolve(strict=...) 與 is_relative_to）只出現在
  safe_resolve 內，全檔各僅一次；
- 全檔不再有 `X in/not in Y.parents` 這種 containment 比對；
- read_file / read_notes / zip_workspace / append_note 皆呼叫 safe_resolve；
- tools.py 也不自寫 containment，改呼叫 safe_resolve。
"""

from __future__ import annotations

import ast
from pathlib import Path

from studio import tools, workspace

WS_SRC = Path(workspace.__file__).read_text(encoding="utf-8")
WS_TREE = ast.parse(WS_SRC)


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函式 {name}")


def _enclosing_func_names(tree: ast.AST, predicate) -> list[str]:
    """回傳『內部含有符合 predicate 之節點』的所有函式名。"""
    names = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and any(predicate(n) for n in ast.walk(fn)):
            names.append(fn.name)
    return names


def _is_strict_resolve(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resolve"
        and any(kw.arg == "strict" for kw in node.keywords)
    )


def _is_is_relative_to(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "is_relative_to"
    )


def _is_parents_containment(node: ast.AST) -> bool:
    # 比對形如  X in Y.parents / X not in Y.parents
    if not isinstance(node, ast.Compare):
        return False
    for op, comp in zip(node.ops, node.comparators, strict=False):
        if isinstance(op, (ast.In, ast.NotIn)):
            if isinstance(comp, ast.Attribute) and comp.attr == "parents":
                return True
    return False


# --- 單一真實來源 ---


def test_strict_resolve_only_in_safe_resolve():
    hosts = _enclosing_func_names(WS_TREE, _is_strict_resolve)
    assert hosts == ["safe_resolve"], f"strict resolve 不應出現在: {hosts}"


def test_is_relative_to_only_in_safe_resolve():
    hosts = _enclosing_func_names(WS_TREE, _is_is_relative_to)
    assert hosts == ["safe_resolve"], f"is_relative_to 不應出現在: {hosts}"


def test_no_parents_containment_anywhere():
    hosts = _enclosing_func_names(WS_TREE, _is_parents_containment)
    assert hosts == [], f"仍有 `in .parents` containment 比對殘留於: {hosts}"


# --- 三處（+append_note）皆呼叫 safe_resolve ---


def _calls_safe_resolve(fn: ast.FunctionDef) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "safe_resolve"
        for n in ast.walk(fn)
    )


def test_callers_delegate_to_safe_resolve():
    for name in ("read_file", "read_notes", "zip_workspace", "append_note"):
        assert _calls_safe_resolve(_func(WS_TREE, name)), f"{name} 未呼叫 safe_resolve"


def test_safe_resolve_defined_once():
    defs = [
        n for n in ast.walk(WS_TREE) if isinstance(n, ast.FunctionDef) and n.name == "safe_resolve"
    ]
    assert len(defs) == 1


def test_tools_safe_path_delegates_and_no_inline_containment():
    tools_tree = ast.parse(Path(tools.__file__).read_text(encoding="utf-8"))
    # 不自寫 containment
    assert _enclosing_func_names(tools_tree, _is_strict_resolve) == []
    assert _enclosing_func_names(tools_tree, _is_is_relative_to) == []
    assert _enclosing_func_names(tools_tree, _is_parents_containment) == []
    # _safe_path 呼叫 safe_resolve
    assert _calls_safe_resolve(_func(tools_tree, "_safe_path"))
