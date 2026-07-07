from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / ".qa_artifacts" / "task3" / "superseded-decision-inventory-2026-07-08.md"
TARGET_AUTHORITY = "docs/task3-authoritative-decision-2026-07-08.md"
REQUIRED_LITERALS = {"778ced", "rerun-765f1b"}
MISSING_TOKEN = "<訊息流未明列>"
LOCATION_TOKEN = "訊息流明列值／查無 repo 實體檔"


def _inventory_rows() -> list[tuple[str, str, str, str]]:
    text = INVENTORY.read_text(encoding="utf-8")
    rows: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if len(cells) == 4 and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_task1_inventory_collects_five_superseded_task3_decisions_verbatim():
    assert INVENTORY.exists(), f"缺 task #1 蒐集清單：{INVENTORY.relative_to(ROOT)}"
    text = INVENTORY.read_text(encoding="utf-8")

    assert TARGET_AUTHORITY in text
    assert "不展開" in text and "不推算" in text

    rows = _inventory_rows()
    assert len(rows) == 5, "task #1 驗收要求逐字蒐集五份被作廢 task3 決議檔"

    literals = {row[1] for row in rows}
    assert REQUIRED_LITERALS <= literals
    assert [row[1] for row in rows[2:]] == [MISSING_TOKEN, MISSING_TOKEN, MISSING_TOKEN]
    assert "99f330…9d3b" in text, "含省略號的 hash 必須逐字保留 U+2026"


def test_task1_inventory_records_path_or_message_flow_for_every_collected_value():
    rows = _inventory_rows()
    assert rows, "蒐集表不可為空"

    for _, literal, hash_relation, location in rows:
        assert hash_relation, f"{literal} 缺 hash 關聯欄位"
        assert location == LOCATION_TOKEN


def test_task1_inventory_preserves_ellipsis_and_does_not_invent_full_sha256():
    raw = INVENTORY.read_bytes()
    text = raw.decode("utf-8")

    assert b"\xe2\x80\xa6" in raw, "省略號必須是 U+2026，不可消失"
    assert not re.search(rb"[0-9A-Fa-f]{2,}\.\.\.[0-9A-Fa-f]{2,}", raw), (
        "不可把 hash 或識別值中的省略號改成三個句點"
    )
    assert not re.search(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])", text), (
        "task #1 是逐字蒐集，不得自行展開或推算 64 碼 sha256"
    )
