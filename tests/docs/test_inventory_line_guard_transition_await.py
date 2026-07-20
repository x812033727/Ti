from __future__ import annotations

import ast
import re

from _repo import REPO_ROOT

INVENTORY = REPO_ROOT / "studio" / "docs" / "transition_await_inventory.md"
ORCHESTRATOR = REPO_ROOT / "studio" / "orchestrator.py"

TRANSITION_HELPERS = (
    "_integrate_wave",
    "_merge_lane",
    "_serialize_lane_rerun",
    "_resolve_conflict_in_lane",
    "_merge_resolved_lane_back",
)

BOUNDARY_NEEDLES = (
    "first = await asyncio.wait_for(",
    "self._demo = await self._final_demo()",
    "results = await asyncio.gather(",
    "all_ok = await self._integrate_wave(",
    "async def _integrate_wave(",
    "async def _stage_demo(",
)


def _source() -> tuple[str, list[str], ast.Module]:
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    return text, text.splitlines(), ast.parse(text)


def _line_with(lines: list[str], needle: str) -> int:
    matches = [idx for idx, line in enumerate(lines, start=1) if needle in line]
    assert len(matches) == 1, f"錨點必須唯一：{needle!r} -> {matches}"
    return matches[0]


def _async_function_map(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}


def _documented_table_lines(markdown: str) -> set[int]:
    return {
        int(match.group(1))
        for match in re.finditer(r"^\| studio/orchestrator\.py:(\d+) \|", markdown, re.M)
    }


def test_transition_boundary_line_numbers_match_live_code() -> None:
    markdown = INVENTORY.read_text(encoding="utf-8")
    _, lines, _ = _source()

    for needle in BOUNDARY_NEEDLES:
        lineno = _line_with(lines, needle)
        assert f"studio/orchestrator.py:{lineno}" in markdown


def test_transition_await_table_lists_live_integration_chain() -> None:
    markdown = INVENTORY.read_text(encoding="utf-8")
    _, lines, tree = _source()
    funcs = _async_function_map(tree)

    required = {_line_with(lines, needle) for needle in BOUNDARY_NEEDLES[:4]}
    for helper in TRANSITION_HELPERS:
        assert helper in funcs, f"找不到過渡段 helper：{helper}"
        required.update(
            node.lineno for node in ast.walk(funcs[helper]) if isinstance(node, ast.Await)
        )

    missing = sorted(required - _documented_table_lines(markdown))
    assert not missing, "transition await 表格缺少現碼行號：" + ", ".join(
        f"studio/orchestrator.py:{lineno}" for lineno in missing
    )
