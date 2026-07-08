"""QA 驗收：任務 #2 fake_push / fake_push_base 簽名需跟新 _push API 對齊。

新 _push / _push_base 會用 keyword-only env 傳 git 認證環境。測試替身若仍是舊簽名，
monkeypatch 後會在非快樂路徑才炸 TypeError；這裡全域掃 tests，避免漏改。
"""

from __future__ import annotations

import ast
from pathlib import Path

PUSH_ATTRS = {"_push", "_push_base"}
TARGET_STUBS = {"fake_push", "fake_push_base"}


def _env_has_default(args: ast.arguments, env_index: int) -> bool:
    defaults = args.defaults
    positional = args.posonlyargs + args.args
    first_default_index = len(positional) - len(defaults)
    return env_index >= first_default_index


def _accepts_env_keyword(args: ast.arguments) -> bool:
    if args.kwarg is not None:
        return True

    positional = args.posonlyargs + args.args
    for index, arg in enumerate(positional):
        if arg.arg == "env":
            return _env_has_default(args, index)

    for index, arg in enumerate(args.kwonlyargs):
        if arg.arg == "env":
            return args.kw_defaults[index] is not None

    return False


def _patched_push_stub_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "setattr":
            continue
        if len(node.args) < 3:
            continue

        attr = node.args[1]
        replacement = node.args[2]
        if (
            isinstance(attr, ast.Constant)
            and attr.value in PUSH_ATTRS
            and isinstance(replacement, ast.Name)
        ):
            names.add(replacement.id)
    return names


def _iter_push_stubs():
    tests_root = Path(__file__).resolve().parent
    for path in sorted(tests_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        target_names = TARGET_STUBS | _patched_push_stub_names(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in target_names
            ):
                yield path, node.lineno, node.name, node.args


def test_all_push_stubs_accept_new_env_keyword():
    failures = []
    seen = 0

    for path, line, name, args in _iter_push_stubs():
        seen += 1
        if not _accepts_env_keyword(args):
            rel = path.relative_to(Path(__file__).resolve().parents[1])
            failures.append(f"{rel}:{line} {name} must accept env= or **kwargs")

    assert seen >= 10, "未掃到預期數量的 publisher._push / _push_base 替身，測試可能失效"
    assert not failures, "\n".join(failures)
