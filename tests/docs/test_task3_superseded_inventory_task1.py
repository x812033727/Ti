from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / ".qa_artifacts" / "task3" / "superseded-decision-inventory-2026-07-08.md"
SOURCE_DOC = ROOT / "docs" / "release-e2e-authoritative-declaration-2026-07-08.md"
TARGET_AUTHORITY = "docs/task3-authoritative-decision-2026-07-08.md"

EXPECTED_CONFIRMED_ROWS = [
    (
        "1",
        "`778ced`",
        "決議檔識別碼",
        "訊息流未明列；不得推算",
        "訊息流明列值；repo 內未找到對應實體決議檔",
    ),
    (
        "2",
        "`rerun-765f1b`",
        "重跑決議檔識別碼",
        "訊息流未明列；不得推算",
        "訊息流明列值；repo 內未找到對應實體決議檔",
    ),
]

EXCLUDED_RELEASE_VALUES = ["`c2f4bb`", "`725cf1`", "`99f330…9d3b`"]


def _table_rows(text: str) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if len(cells) == 5 and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_task3_superseded_inventory_reports_only_confirmed_task3_values():
    assert INVENTORY.exists(), f"缺 task #1 蒐集清單：{INVENTORY.relative_to(ROOT)}"
    text = INVENTORY.read_text(encoding="utf-8")

    assert TARGET_AUTHORITY in text
    assert "來源限定為本輪 QA 訊息流與 repo 內已存在的宣告檔" in text
    assert "不展開、不推算" in text
    assert "目前可確認的 task3 被作廢決議檔為 2 份" in text
    assert "不得以其他任務線的值湊滿五份" in text

    rows = _table_rows(text)
    assert rows == EXPECTED_CONFIRMED_ROWS


def test_task3_superseded_inventory_preserves_ellipsis_and_does_not_expand_hashes():
    raw = INVENTORY.read_bytes()
    text = raw.decode("utf-8")

    assert b"99f330\xe2\x80\xa69d3b" in raw, "省略號必須是 U+2026"
    assert b"99f330...9d3b" not in raw, "不可把省略號改成三個句點"
    assert "禁止改成三個句點 `...`" in text
    assert "不得推算或借用 release-e2e 值" in text
    assert not re.search(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])", text)


def test_task3_superseded_inventory_records_repo_paths_or_message_flow_status():
    text = INVENTORY.read_text(encoding="utf-8")
    rows = _table_rows(text)

    message_only = [row for row in rows if "訊息流明列值" in row[4]]

    assert [row[1] for row in message_only] == ["`778ced`", "`rerun-765f1b`"]
    assert all("repo 內未找到對應實體決議檔" in row[4] for row in message_only)


def test_task3_superseded_inventory_does_not_count_release_e2e_hash_as_file():
    text = INVENTORY.read_text(encoding="utf-8")

    assert SOURCE_DOC.exists(), f"缺既有範式來源：{SOURCE_DOC.relative_to(ROOT)}"
    source_text = SOURCE_DOC.read_text(encoding="utf-8")
    for literal in EXCLUDED_RELEASE_VALUES:
        assert literal in text
        assert literal.strip("`") in source_text

    assert "`99f330…9d3b`：出現於 `docs/release-e2e-authoritative-declaration-2026-07-08.md`" in text
    assert "它是 hash 屬性，不是另一份獨立決議檔" in text
