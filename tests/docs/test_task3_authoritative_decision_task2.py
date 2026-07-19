from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET_REL = "docs/task3-authoritative-decision-2026-07-08.md"
TARGET = ROOT / TARGET_REL
INVENTORY_REL = ".qa_artifacts/task3/superseded-decision-inventory-2026-07-08.md"
INVENTORY = ROOT / INVENTORY_REL
MISSING_TOKEN = "<訊息流未明列>"
LOCATION_TOKEN = "訊息流明列值／查無 repo 實體檔"
SUPERSEDED = f"Superseded by {TARGET_REL}"

EXPECTED_ROWS = [
    ("1", "778ced", MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
    ("2", "rerun-765f1b", MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
    ("3", MISSING_TOKEN, MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
    ("4", MISSING_TOKEN, MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
    ("5", MISSING_TOKEN, MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
]


def _authority_text() -> str:
    assert TARGET.exists(), f"缺唯一權威決議檔：{TARGET_REL}"
    text = TARGET.read_text(encoding="utf-8")
    assert text.strip(), "權威決議檔不可為空"
    return text


def _decision_rows(text: str) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if len(cells) == 5 and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_task2_authoritative_decision_exists_only_at_pinned_path():
    matches = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "docs").rglob("task3-authoritative-decision-2026-07-08.md")
    ]

    assert matches == [TARGET_REL]
    text = _authority_text()
    assert "additive 權威決議" in text
    assert "不覆寫既有" in text
    assert "`docs/release-e2e-closure-report.md`" in text
    assert "`docs/release-e2e-handoff.md`" in text
    assert "`docs/evidence/`" in text


def test_task2_supersedes_exactly_five_message_flow_decisions_verbatim():
    text = _authority_text()

    assert INVENTORY.exists(), f"缺 task #1 來源清單：{INVENTORY_REL}"
    assert f"- 來源：`{INVENTORY_REL}`" in text
    assert "五列採固定占位契約，不查找、不展開、不推算" in text

    rows = _decision_rows(text)
    assert rows == EXPECTED_ROWS
    assert text.count(SUPERSEDED) == 5
    assert "778ced" in text
    assert "rerun-765f1b" in text
    assert [row[1] for row in rows[2:]] == [MISSING_TOKEN, MISSING_TOKEN, MISSING_TOKEN]


def test_task2_records_linking_and_immutability_for_missing_original_files():
    text = _authority_text()

    assert "## 雙向連結與 immutability" in text
    assert "權威檔到被作廢端" in text
    assert "被作廢端到權威檔" in text
    assert "若 repo 內存在原決議檔，該端應回指本權威檔" in text
    assert "五列皆查無 repo 實體檔" in text
    assert "不新增、不改寫、不偽造原檔" in text


def test_task2_distinguishes_authoritative_file_sha_from_embedded_hashes():
    text = _authority_text()
    raw = TARGET.read_bytes()

    assert "整檔 sha256（權威）" in text
    assert "檔內嵌 hash（僅自證）" in text
    assert "此值不可固定嵌回本檔" in text
    assert "不能取代整檔 sha256" in text
    assert "不能被展開或推算成 64-hex" in text
    assert b"99f330\xe2\x80\xa69d3b" in raw, "省略號必須保留為 U+2026"
    assert b"99f330...9d3b" not in raw, "不可把省略號換成三個句點"


def test_task2_reports_relative_path_that_can_be_read_programmatically():
    text = _authority_text()
    match = re.search(r"QA 回報權威檔相對路徑：`([^`]+)`", text)

    assert match, "缺 QA 回報權威檔相對路徑"
    reported = match.group(1)
    reported_path = ROOT / reported

    assert reported == TARGET_REL
    assert reported_path.exists()
    assert reported_path.read_bytes() == TARGET.read_bytes()


def test_task2_documents_atomic_write_without_new_dependencies():
    text = _authority_text()

    assert "## 原子落盤紀錄" in text
    assert "暫存檔" in text
    assert "flush" in text
    assert "os.fsync" in text
    assert "os.replace" in text
    assert "fsync` 父目錄" in text
    assert "未新增第三方依賴" in text
    assert "atomicwrites" not in text


def test_task2_self_check_commands_use_python3_not_bare_python():
    text = _authority_text()

    assert ".venv/bin/python -m pytest tests/docs -q" in text
    assert "python3 - <<'PY'" in text
    for line in text.splitlines():
        assert not re.search(
            r"(?<![./A-Za-z0-9_-])python(?![A-Za-z0-9_-])", line
        ), f"不得使用裸 python 指令：{line}"
