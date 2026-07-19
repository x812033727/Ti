"""AST guard for real-server subprocess proxy env scrubbing."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tracked_real_server_tests() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "tests/server/test_*real_server*.py"],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    return [ROOT / line for line in result.stdout.splitlines()]


def _collect_subprocess_imports(tree: ast.AST) -> tuple[set[str], set[str]]:
    subprocess_modules = {"subprocess"}
    subprocess_call_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in {"Popen", "run"}:
                    subprocess_call_names.add(alias.asname or alias.name)

    return subprocess_modules, subprocess_call_names


def _collect_scrub_imports(tree: ast.AST) -> set[str]:
    scrub_names: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != "_real_server_client":
            continue
        for alias in node.names:
            if alias.name == "scrub_proxy_env":
                scrub_names.add(alias.asname or alias.name)

    return scrub_names


def _is_subprocess_call(
    call: ast.Call,
    subprocess_modules: set[str],
    subprocess_call_names: set[str],
) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr in {"Popen", "run"} and func.value.id in subprocess_modules
    if isinstance(func, ast.Name):
        return func.id in subprocess_call_names
    return False


def _has_keyword(call: ast.Call, keyword: str) -> bool:
    return any(item.arg == keyword for item in call.keywords)


def _calls_scrub_proxy_env(tree: ast.AST, scrub_names: set[str]) -> bool:
    if not scrub_names:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in scrub_names:
                return True
    return False


def _collect_violations(source: str, filename: str = "<source>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    subprocess_modules, subprocess_call_names = _collect_subprocess_imports(tree)
    scrub_names = _collect_scrub_imports(tree)
    subprocess_calls: list[ast.Call] = []
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_subprocess_call(
            node,
            subprocess_modules,
            subprocess_call_names,
        ):
            subprocess_calls.append(node)
            if not _has_keyword(node, "env"):
                violations.append(f"{filename}:L{node.lineno}: subprocess call missing env=")

    if not subprocess_calls:
        return []

    if not scrub_names:
        violations.append(f"{filename}: missing scrub_proxy_env import")
    if not _calls_scrub_proxy_env(tree, scrub_names):
        violations.append(f"{filename}: missing scrub_proxy_env call")

    return violations


def test_real_server_subprocesses_scrub_proxy_env() -> None:
    paths = _tracked_real_server_tests()
    path_names = {path.name for path in paths}
    assert {"test_smoke_agenda_real_server.py", "test_ws_attach_real_server.py"} <= path_names

    violations: list[str] = []
    for path in paths:
        relative_path = path.relative_to(ROOT).as_posix()
        violations.extend(_collect_violations(path.read_text(encoding="utf-8"), relative_path))

    assert not violations, "\n".join(violations)


def test_proxy_scrub_guard_rejects_missing_scrub_call() -> None:
    source = (
        "import os\n"
        "import subprocess\n"
        "def test_real_server(tmp_path):\n"
        "    env = os.environ.copy()\n"
        "    subprocess.Popen(['server'], env=env)\n"
        "    subprocess.run(['client'], env=env)\n"
    )

    assert _collect_violations(source) == [
        "<source>: missing scrub_proxy_env import",
        "<source>: missing scrub_proxy_env call",
    ]


def test_proxy_scrub_guard_rejects_missing_client_env() -> None:
    source = (
        "import os\n"
        "import subprocess\n"
        "from _real_server_client import scrub_proxy_env\n"
        "def test_real_server(tmp_path):\n"
        "    env = os.environ.copy()\n"
        "    scrub_proxy_env(env)\n"
        "    subprocess.Popen(['server'], env=env)\n"
        "    subprocess.run(['client'])\n"
    )

    assert _collect_violations(source) == ["<source>:L8: subprocess call missing env="]


def test_proxy_scrub_guard_accepts_scrubbed_subprocesses() -> None:
    source = (
        "import os\n"
        "import subprocess\n"
        "from _real_server_client import scrub_proxy_env\n"
        "def test_real_server(tmp_path):\n"
        "    env = os.environ.copy()\n"
        "    scrub_proxy_env(env)\n"
        "    subprocess.Popen(['server'], env=env)\n"
        "    subprocess.run(['client'], env=env)\n"
    )

    assert _collect_violations(source) == []
