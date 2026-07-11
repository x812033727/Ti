"""AST guard for loopback smoke HTTP client policy.

Policy: loopback 必關 trust_env／proxy；外網 client 不在關閉環境 proxy 範圍
（見 CLAUDE.md 雙軌決策）。本測試用 AST 掃 tests/server/smoke_*.py 實際 Call 節點，
避免字串檢查漏掉「同檔多個 client 只關一個」。

Known limits: only attribute-access calls such as httpx.AsyncClient(...) are tracked;
trust_env=SOME_VAR / proxy=SOME_VAR 這類變數傳入不在 AST 守門範圍；urllib loopback
client 不在本 httpx/websockets 守門範圍。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
MISSING = object()
CLIENT_CALLS = {
    "httpx.AsyncClient",
    "httpx.Client",
    "httpx.get",
    "httpx.post",
    "websockets.connect",
}


@dataclass(frozen=True)
class ClientCall:
    path: Path
    line: int
    call: str
    node: ast.Call


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    call: str
    expected: str


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def _keyword_value(node: ast.Call, name: str) -> object:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return MISSING


def _is_literal_false(value: object) -> bool:
    return isinstance(value, ast.Constant) and value.value is False


def _is_literal_none(value: object) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def _find_client_calls(path: Path) -> list[ClientCall]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[ClientCall] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node)
        if call in CLIENT_CALLS:
            calls.append(ClientCall(path, node.lineno, call, node))

    return calls


def _find_proxy_policy_violations(path: Path) -> list[Violation]:
    violations: list[Violation] = []

    for client_call in _find_client_calls(path):
        if client_call.call.startswith("httpx."):
            if not _is_literal_false(_keyword_value(client_call.node, "trust_env")):
                violations.append(
                    Violation(
                        client_call.path,
                        client_call.line,
                        client_call.call,
                        "trust_env=False",
                    )
                )
        elif client_call.call == "websockets.connect":
            if not _is_literal_none(_keyword_value(client_call.node, "proxy")):
                violations.append(
                    Violation(
                        client_call.path,
                        client_call.line,
                        client_call.call,
                        "proxy=None",
                    )
                )

    return violations


def _format_violations(violations: list[Violation]) -> str:
    return "\n".join(
        f"{violation.path}:{violation.line}: {violation.call} missing {violation.expected}"
        for violation in violations
    )


def _relative(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def test_loopback_smoke_clients_do_not_use_host_proxy_env_ast() -> None:
    smoke_paths = sorted(HERE.glob("smoke_*.py"))
    assert len(smoke_paths) >= 2

    violations = [
        violation for path in smoke_paths for violation in _find_proxy_policy_violations(path)
    ]

    assert not violations, _format_violations(violations)


def test_black_sample_requires_every_client_to_disable_proxy_env(tmp_path: Path) -> None:
    bad_smoke = tmp_path / "smoke_bad.py"
    bad_smoke.write_text(
        "\n".join(
            [
                "import httpx",
                "",
                "async def main():",
                "    async with httpx.AsyncClient(trust_env=False) as ok:",
                "        pass",
                "    async with httpx.AsyncClient() as bad:",
                "        pass",
                "    async with httpx.Client(trust_env=0) as also_bad:",
                "        pass",
                "    async with websockets.connect('ws://127.0.0.1:8000/ws') as ws:",
                "        pass",
            ]
        ),
        encoding="utf-8",
    )

    violations = _find_proxy_policy_violations(bad_smoke)

    assert [(violation.line, violation.call, violation.expected) for violation in violations] == [
        (6, "httpx.AsyncClient", "trust_env=False"),
        (8, "httpx.Client", "trust_env=False"),
        (10, "websockets.connect", "proxy=None"),
    ]
