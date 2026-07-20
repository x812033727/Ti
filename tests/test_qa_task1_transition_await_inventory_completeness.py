"""QA 補強：過渡段 await 清單必須覆蓋 helper chain 的每個 await 行號。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "studio" / "docs" / "transition_await_inventory.md"
ORCHESTRATOR = ROOT / "studio" / "orchestrator.py"

TRANSITION_HELPERS = (
    "_integrate_wave",
    "_merge_lane",
    "_serialize_lane_rerun",
    "_resolve_conflict_in_lane",
    "_merge_resolved_lane_back",
)

BOUNDARY_AWAIT_NEEDLES = (
    "results = await asyncio.gather(",
    "all_ok = await self._integrate_wave(",
    "first = await asyncio.wait_for(",
    "self._demo = await self._final_demo()",
)


def _source() -> tuple[str, list[str], ast.Module]:
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    return text, text.splitlines(), ast.parse(text)


def _async_function_map(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}


def _line_with(lines: list[str], needle: str) -> int:
    matches = [idx for idx, line in enumerate(lines, start=1) if needle in line]
    assert len(matches) == 1, f"{needle!r} 應唯一定位，實際：{matches}"
    return matches[0]


def _table_line_numbers() -> set[int]:
    text = INVENTORY.read_text(encoding="utf-8")
    return {
        int(match.group(1))
        for match in re.finditer(r"^\| studio/orchestrator\.py:(\d+) \|", text, re.M)
    }


def test_transition_inventory_lists_every_await_in_integration_chain():
    _, lines, tree = _source()
    funcs = _async_function_map(tree)
    documented = _table_line_numbers()

    required: set[int] = {_line_with(lines, needle) for needle in BOUNDARY_AWAIT_NEEDLES}
    for helper in TRANSITION_HELPERS:
        assert helper in funcs, f"找不到過渡段 helper：{helper}"
        required.update(
            node.lineno for node in ast.walk(funcs[helper]) if isinstance(node, ast.Await)
        )

    missing = sorted(required - documented)
    assert not missing, "transition_await_inventory.md 未逐行列出過渡段 await：" + ", ".join(
        f"studio/orchestrator.py:{line}" for line in missing
    )
