import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = ROOT / "ARCHITECTURE.md"

TASK_PARALLEL_HEADING = "任務並行（多支線 lane，預設開啟）"
CONTRACT_HEADING = "baseline 注入契約"
PRIORITY = "`顯式注入 > env(TI_*) > lane manifest > 模組 DEFAULT`"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str, level: int) -> str:
    marker = "#" * level
    match = re.search(
        rf"^{re.escape(marker + ' ' + heading)}\n(?P<body>.*?)(?=^{marker} |\Z)",
        text,
        re.M | re.S,
    )
    assert match, f"找不到章節: {heading}"
    return match.group("body")


def _contract_section() -> str:
    task_parallel = _section(_read(ARCHITECTURE), TASK_PARALLEL_HEADING, level=2)
    return _section(task_parallel, CONTRACT_HEADING, level=3)


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        normalized = line.replace("|", "").strip()
        if normalized and set(normalized) <= {"-", ":"}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_baseline_contract_is_nested_in_task_parallel_after_lane_context() -> None:
    architecture = _read(ARCHITECTURE)
    task_parallel = _section(architecture, TASK_PARALLEL_HEADING, level=2)
    heading = f"### {CONTRACT_HEADING}"

    assert architecture.count(heading) == 1
    assert heading in task_parallel
    assert task_parallel.index("每條支線（lane）由 `LaneContext` 隔離") < task_parallel.index(
        heading
    )
    assert task_parallel.index("全域 `TI_LLM_MAX_CONCURRENCY`") < task_parallel.index(heading)


def test_first_sentence_disambiguates_lane_baseline_from_gitignore_baseline() -> None:
    contract = _contract_section()
    first_line = next(line.strip() for line in contract.splitlines() if line.strip())

    assert first_line.startswith("此 baseline 指")
    assert "lane 啟動設定基準" in first_line
    assert "env + manifest" in first_line
    assert "`write_baseline_gitignore`" in first_line
    assert "發佈前 .gitignore 淨化" in first_line
    assert "同名不同源" in first_line
    assert "前瞻契約" in first_line
    assert "尚未落地" in first_line


def test_priority_is_single_ordered_ssot_and_matches_env_over_file_rule() -> None:
    contract = _contract_section()
    priority_line = next(line.strip() for line in contract.splitlines() if PRIORITY in line)

    assert contract.count(PRIORITY) == 1
    assert "優先序（單一有序清單）" in priority_line
    assert "`config.py`" in priority_line
    assert "`settings.py`" in priority_line
    assert "env 覆蓋檔案" in priority_line


def test_fail_strategy_table_is_three_column_and_has_repo_examples() -> None:
    contract = _contract_section()
    rows = _table_rows(contract)

    assert rows[0] == ["注入項類別", "缺失/非法時行為", "依據"]
    assert len(rows) >= 3
    for row in rows:
        assert len(row) == 3

    data = rows[1:]
    assert all(("fail-open" in row[1] or "fail-closed" in row[1]) for row in data)

    autopilot_rows = [row for row in data if "`AUTOPILOT_REPO`" in " ".join(row)]
    discuss_rows = [row for row in data if "`TI_DISCUSS_MODE`" in " ".join(row)]
    assert autopilot_rows, "決策表缺少 AUTOPILOT_REPO repo 實例"
    assert discuss_rows, "決策表缺少 TI_DISCUSS_MODE repo 實例"

    autopilot_row = autopilot_rows[0]
    discuss_row = discuss_rows[0]
    assert "fail-closed" in autopilot_row[1]
    assert "類比佐證" in autopilot_row[2]
    assert "fail-open" in discuss_row[1]
    assert "類比佐證" in discuss_row[2]

    assert "非 lane 注入層現有行為" in contract
    assert "尚無守門測試覆蓋" in contract
