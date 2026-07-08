import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = ROOT / "ARCHITECTURE.md"
DECISIONS = ROOT / "DECISIONS.md"
INVENTORY = ROOT / "studio/docs/lane_baseline_injection_inventory.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str, level: int = 2) -> str:
    marker = "#" * level
    next_marker = "#" * level
    match = re.search(
        rf"^{re.escape(marker + ' ' + heading)}\n(?P<body>.*?)(?=^{next_marker} |\Z)",
        text,
        re.M | re.S,
    )
    assert match, f"找不到章節: {heading}"
    return match.group("body")


def _subsection(parent: str, heading: str) -> str:
    match = re.search(
        rf"^### {re.escape(heading)}\n(?P<body>.*?)(?=^### |^## |\Z)",
        parent,
        re.M | re.S,
    )
    assert match, f"找不到子段: {heading}"
    return match.group("body")


def _decision_section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"找不到 ADR: {title}"
    return match.group("body")


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_architecture_has_baseline_contract_with_disambiguation_and_priority() -> None:
    task_parallel = _section(_read(ARCHITECTURE), "任務並行（多支線 lane，預設開啟）")
    contract = _subsection(task_parallel, "baseline 注入契約")
    first_sentence = next(line.strip() for line in contract.splitlines() if line.strip())

    assert "lane 啟動設定基準" in first_sentence
    assert "env" in first_sentence
    assert "manifest" in first_sentence
    assert "`write_baseline_gitignore`" in first_sentence
    assert "發佈前" in first_sentence or ".gitignore" in first_sentence

    assert "`顯式注入 > env(TI_*) > lane manifest > 模組 DEFAULT`" in contract
    assert "`config.py`" in contract
    assert "`settings.py`" in contract
    assert "env 覆蓋檔案" in contract


def test_architecture_decision_table_is_three_columns_and_names_fail_strategy() -> None:
    task_parallel = _section(_read(ARCHITECTURE), "任務並行（多支線 lane，預設開啟）")
    contract = _subsection(task_parallel, "baseline 注入契約")
    rows = _table_rows(contract)

    assert rows, "baseline 注入契約必須有決策表"
    assert rows[0] == ["注入項類別", "缺失/非法時行為", "依據"]
    assert all(len(row) == 3 for row in rows)
    assert all(("fail-open" in row[1] or "fail-closed" in row[1]) for row in rows[1:])

    table_text = "\n".join("|".join(row) for row in rows[1:])
    assert re.search(r"AUTOPILOT_REPO.*fail-closed|fail-closed.*AUTOPILOT_REPO", table_text, re.S)
    assert re.search(r"TI_DISCUSS_MODE.*fail-open|fail-open.*TI_DISCUSS_MODE", table_text, re.S)


def test_architecture_and_adr_have_bidirectional_links_and_handoff_todo() -> None:
    architecture = _read(ARCHITECTURE)
    decisions = _read(DECISIONS)
    task_parallel = _section(architecture, "任務並行（多支線 lane，預設開啟）")
    contract = _subsection(task_parallel, "baseline 注入契約")

    title_match = re.search(r"見 DECISIONS\.md『([^』]+)』", contract)
    assert title_match, "ARCHITECTURE 子段末需回指 DECISIONS.md ADR 標題"
    adr_title = title_match.group(1)
    adr = _decision_section(decisions, adr_title)

    assert "- 時間：" in adr
    assert "- 理由：" in adr
    assert "- 否決方案：" in adr
    assert "ARCHITECTURE.md" in adr
    assert "任務並行" in adr
    assert "baseline 注入契約" in adr
    assert "lane 注入層落地後補守門測試對齊決策表" in contract
    assert "lane 注入層落地後補守門測試對齊決策表" in adr


def test_decisions_has_no_baseline_design_note_drift() -> None:
    decisions = _read(DECISIONS)
    leaked_design_notes = [
        "技術選型＝純文件變更",
        "模組邊界＝ARCHITECTURE.md「任務並行」節",
        "子段開頭第一句為 baseline 消歧句",
        "決策表為三欄",
        "雙向互鏈以「節標題文字」為錨",
        "驗證邊界＝`tests/docs` 為唯一 gate",
    ]

    for note in leaked_design_notes:
        assert note not in decisions


def test_inventory_remains_descriptive_and_feeds_contract_decision_table() -> None:
    inventory = _read(INVENTORY)

    assert "供 ARCHITECTURE.md「baseline 注入契約」決策表對齊" in inventory
    assert "目前實作" in inventory
    assert "不等同已落地的 lane baseline env/manifest 契約" in inventory
    assert "`LaneContext`" in inventory
    assert "`_integrate_wave()`" in inventory
    assert "`AUTOPILOT_REPO`" in inventory
    assert "fail-closed 類比" in inventory
    assert "`TI_DISCUSS_MODE`" in inventory
    assert "fail-open 類比" in inventory
