"""AST guard for loopback smoke clients.

Direct websockets.connect calls are rejected; real-server smoke clients must use
_loopback_clients.loopback_websocket_connect for websockets-version compatibility.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _is_constant(node: ast.AST, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _keyword_matches(call: ast.Call, keyword: str, value: object) -> bool:
    for item in call.keywords:
        if item.arg == keyword and _is_constant(item.value, value):
            return True
    return False


def _collect_imports(tree: ast.AST) -> tuple[set[str], set[str], set[str], set[str]]:
    httpx_modules = {"httpx"}
    httpx_clients: set[str] = set()
    websocket_modules = {"websockets"}
    websocket_connects: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    httpx_modules.add(alias.asname or alias.name)
                elif alias.name == "websockets":
                    websocket_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if node.module == "httpx" and alias.name == "AsyncClient":
                    httpx_clients.add(alias.asname or alias.name)
                elif node.module == "websockets" and alias.name == "connect":
                    websocket_connects.add(alias.asname or alias.name)

    return httpx_modules, httpx_clients, websocket_modules, websocket_connects


def _call_type(
    call: ast.Call,
    httpx_modules: set[str],
    httpx_clients: set[str],
    websocket_modules: set[str],
    websocket_connects: set[str],
) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.attr == "AsyncClient" and func.value.id in httpx_modules:
            return "httpx.AsyncClient"
        if func.attr == "connect" and func.value.id in websocket_modules:
            return "websockets.connect"
    if isinstance(func, ast.Name):
        if func.id in httpx_clients:
            return "httpx.AsyncClient"
        if func.id in websocket_connects:
            return "websockets.connect"
    return None


def _collect_violations(source: str, filename: str = "<source>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    httpx_modules, httpx_clients, websocket_modules, websocket_connects = _collect_imports(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_type = _call_type(
            node,
            httpx_modules,
            httpx_clients,
            websocket_modules,
            websocket_connects,
        )
        if call_type == "httpx.AsyncClient" and not _keyword_matches(node, "trust_env", False):
            violations.append(f"{filename}:L{node.lineno}: httpx.AsyncClient")
        elif call_type == "websockets.connect":
            violations.append(f"{filename}:L{node.lineno}: websockets.connect")

    return violations


def _tracked_server_python_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "tests/server"],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line.endswith(".py")]


def test_loopback_clients_do_not_use_host_proxy_env() -> None:
    violations: list[str] = []
    for path in _tracked_server_python_files():
        relative_path = path.relative_to(ROOT).as_posix()
        violations.extend(_collect_violations(path.read_text(encoding="utf-8"), relative_path))

    assert not violations, "\n".join(violations)


def test_loopback_proxy_guard_rejects_missing_httpx_trust_env() -> None:
    source = "import httpx\nclient = httpx.AsyncClient(timeout=30)\n"

    assert _collect_violations(source) == ["<source>:L2: httpx.AsyncClient"]


def test_loopback_proxy_guard_rejects_direct_websocket_connect() -> None:
    source = (
        "import websockets\n"
        "async def f():\n"
        "    async with websockets.connect('ws://x') as ws:\n"
        "        pass\n"
    )

    assert _collect_violations(source) == ["<source>:L3: websockets.connect"]


def test_loopback_proxy_guard_rejects_direct_websocket_connect_with_proxy_none() -> None:
    source = (
        "import websockets\n"
        "async def f():\n"
        "    async with websockets.connect('ws://x', proxy=None) as ws:\n"
        "        pass\n"
    )

    assert _collect_violations(source) == ["<source>:L3: websockets.connect"]


def test_loopback_proxy_guard_rejects_imported_httpx_client_alias() -> None:
    source = "from httpx import AsyncClient\nclient = AsyncClient(timeout=30)\n"

    assert _collect_violations(source) == ["<source>:L2: httpx.AsyncClient"]
