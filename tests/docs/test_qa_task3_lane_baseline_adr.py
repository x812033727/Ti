import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = ROOT / "ARCHITECTURE.md"
DECISIONS = ROOT / "DECISIONS.md"

TASK_PARALLEL_HEADING = "任務並行（多支線 lane，預設開啟）"
CONTRACT_HEADING = "baseline 注入契約"
ADR_TITLE = "lane baseline 注入契約：env/manifest 優先序與 fail-open/closed 分流策略"
PRIORITY = "`顯式注入 > env(TI_*) > lane manifest > 模組 DEFAULT`"
HANDOFF_TODO = "lane 注入層落地後補守門測試對齊決策表"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, level: int, heading: str) -> str:
    marker = "#" * level
    match = re.search(
        rf"^{re.escape(marker + ' ' + heading)}\n(?P<body>.*?)(?=^{marker} |\Z)",
        text,
        re.M | re.S,
    )
    assert match, f"找不到章節: {heading}"
    return match.group("body")


def _contract_section() -> str:
    task_parallel = _section(_read(ARCHITECTURE), 2, TASK_PARALLEL_HEADING)
    return _section(task_parallel, 3, CONTRACT_HEADING)


def _adr_section() -> str:
    return _section(_read(DECISIONS), 2, ADR_TITLE)


def test_task3_adr_is_single_append_at_decisions_tail() -> None:
    decisions = _read(DECISIONS)

    assert decisions.count(f"## {ADR_TITLE}") == 1
    assert decisions.rstrip().endswith(_adr_section().strip())


def test_task3_adr_keeps_existing_decision_format_and_records_contract() -> None:
    adr = _adr_section()
    lines = [line.strip() for line in adr.splitlines() if line.strip()]

    assert re.fullmatch(r"- 時間：\d{4}-\d{2}-\d{2} \d{2}:\d{2}", lines[0])
    assert any(line.startswith("- 理由：") for line in lines)
    assert any(line.startswith("- 否決方案：") for line in lines)
    assert PRIORITY in adr
    assert "env 覆蓋檔案" in adr
    assert "fail-closed" in adr
    assert "fail-open" in adr
    assert "`AUTOPILOT_REPO`" in adr
    assert "`TI_DISCUSS_MODE`" in adr


def test_architecture_and_task3_adr_are_bidirectionally_linked_by_same_title() -> None:
    contract = _contract_section()
    adr = _adr_section()

    assert f"見 DECISIONS.md『{ADR_TITLE}』" in contract
    assert "ARCHITECTURE.md『任務並行』節「baseline 注入契約」子段" in adr


def test_task3_adr_does_not_claim_unimplemented_lane_injection_is_covered() -> None:
    contract = _contract_section()
    adr = _adr_section()

    assert "前瞻契約" in contract
    assert "lane 注入層尚未落地" in contract
    assert "非 lane 注入層現有行為" in contract
    assert HANDOFF_TODO in contract

    assert "前瞻契約" in adr
    assert "非現有 lane 注入行為" in adr
    assert HANDOFF_TODO in adr
