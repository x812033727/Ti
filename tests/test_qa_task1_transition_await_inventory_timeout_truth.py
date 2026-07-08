"""QA 補強：await 清單的 timeout 狀態必須符合現碼，不只行號正確。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "studio" / "docs" / "transition_await_inventory.md"
WS = ROOT / "studio" / "ws.py"


def _inventory_rows() -> list[list[str]]:
    text = INVENTORY.read_text(encoding="utf-8")
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.startswith("| studio/orchestrator.py:"):
            continue
        rows.append([col.strip() for col in line.strip().strip("|").split("|")])
    return rows


def _ws_broadcast_body() -> str:
    source = WS.read_text(encoding="utf-8")
    match = re.search(
        r"^    async def broadcast\(event: StudioEvent\) -> None:\n"
        r"(?P<body>(?:^ {8}.+\n|^\n)+)",
        source,
        flags=re.M,
    )
    assert match, "找不到 ws.py 的 production broadcast()"
    return match.group("body")


def test_broadcast_rows_do_not_claim_timeout_or_immediate_return():
    """Production broadcast 可能 await websocket.send_json，表格不得宣稱有界或即時返回。"""

    body = _ws_broadcast_body()
    assert "await websocket.send_json(d)" in body
    assert "wait_for(" not in body

    wrong_rows: list[str] = []
    for cols in _inventory_rows():
        assert len(cols) == 5
        location, await_expr, kind, timeout_state, verdict = cols
        if "await self.broadcast" not in await_expr:
            continue
        if "無" not in timeout_state or "即時返回" in timeout_state or verdict == "有界":
            wrong_rows.append(f"{location} | {kind} | timeout={timeout_state} | 判定={verdict}")

    assert not wrong_rows, (
        "broadcast await 無本地 wait_for，清單不可標成即時返回/有界：" + "; ".join(wrong_rows)
    )
