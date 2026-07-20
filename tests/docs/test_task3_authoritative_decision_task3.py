from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET_REL = "docs/task3-authoritative-decision-2026-07-08.md"
TARGET = ROOT / TARGET_REL
MISSING_TOKEN = "<訊息流未明列>"
LOCATION_TOKEN = "訊息流明列值／查無 repo 實體檔"
SUPERSEDED = f"Superseded by {TARGET_REL}"

BARE_PYTHON_RE = re.compile(r"(?<![./A-Za-z0-9_-])python(?![A-Za-z0-9_-])")


def _table_rows(text: str) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if len(cells) == 5 and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_task3_authoritative_decision_exists_once_and_reports_pinned_path():
    matches = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "docs").rglob("task3-authoritative-decision-2026-07-08.md")
    ]

    assert matches == [TARGET_REL]
    assert TARGET.exists()

    text = TARGET.read_text(encoding="utf-8")
    assert text.strip()

    match = re.search(r"QA 回報權威檔相對路徑：`([^`]+)`", text)
    assert match, "缺 QA 回報權威檔相對路徑"

    reported = match.group(1)
    assert reported == TARGET_REL
    assert (ROOT / reported).read_text(encoding="utf-8") == text


def test_task3_authoritative_decision_marks_five_rows_as_superseded_verbatim():
    text = TARGET.read_text(encoding="utf-8")
    raw = TARGET.read_bytes()

    assert _table_rows(text) == [
        ("1", "778ced", MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
        ("2", "rerun-765f1b", MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
        ("3", MISSING_TOKEN, MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
        ("4", MISSING_TOKEN, MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
        ("5", MISSING_TOKEN, MISSING_TOKEN, LOCATION_TOKEN, SUPERSEDED),
    ]
    assert text.count(SUPERSEDED) == 5
    assert "99f330…9d3b" in text
    assert b"99f330\xe2\x80\xa69d3b" in raw, "省略號必須是 U+2026"
    assert b"99f330...9d3b" not in raw, "不可把省略號改成三個句點"
    assert not re.search(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])", text)
    assert "五列採固定占位契約，不查找、不展開、不推算。" in text
    assert "五列皆為 `訊息流明列值／查無 repo 實體檔`。" in text


def test_task3_authoritative_decision_self_check_commands_use_python3_not_bare_python():
    text = TARGET.read_text(encoding="utf-8")

    assert ".venv/bin/python -m pytest tests/docs -q" in text
    assert "python3 - <<'PY'" in text
    for line in text.splitlines():
        assert not BARE_PYTHON_RE.search(line), f"不得出現裸 python：{line}"
