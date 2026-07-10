from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "release-e2e-closure-report.md"
EVIDENCE_DIR = ROOT / "docs" / "evidence"


def _read_report() -> str:
    return REPORT.read_text(encoding="utf-8")


def _load_json(name: str) -> dict:
    return json.loads((EVIDENCE_DIR / name).read_text(encoding="utf-8"))


def _section(report: str, heading: str) -> str:
    start = report.index(heading)
    next_heading = report.find("\n## ", start + len(heading))
    return report[start:] if next_heading == -1 else report[start:next_heading]


def _table_rows(report: str) -> dict[str, list[str]]:
    header = (
        "| # | 閉環環節 | Evidence 檔路徑 | 原 `captured_at_utc` | 關鍵勾稽值 | "
        "本次線上重驗 | 雜湊 / 判定規則 |"
    )
    lines = report.splitlines()
    start = lines.index(header)
    rows: dict[str, list[str]] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("| #"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows[cells[0]] = cells
    return rows


def test_task4_all_match_keeps_closure_and_empty_gap():
    report = _read_report()
    rows = _table_rows(report)
    verdict = _load_json("release-v0.2.0-body-structure-verdict.json")
    smoke = _load_json("release-smoke-v0.2.0-trigger.json")

    row2_recheck = rows["#2"][5]
    row3_recheck = rows["#3"][5]

    task2_required = [
        "2026-07-07",
        f"verdict={verdict['verdict']}",
        "problems=[]",
        "無 mismatch",
        "diff -u",
        "jq -S '{verdict, checks, problems}'",
    ]
    task2_required.extend(
        f"{key}={str(value).lower()}"
        for key, value in verdict["checks"].items()
        if isinstance(value, bool)
    )
    task2_required.append(f"頂部第一個頂層## 區塊={verdict['checks']['頂部第一個頂層## 區塊']}")

    task3_required = [
        "2026-07-07",
        f"run_id={smoke['run_id']}",
        f"event={smoke['event']}",
        f"status={smoke['status']}",
        f"conclusion={smoke['conclusion']}",
        f"workflow_path={smoke['workflow_path']}",
        "無 mismatch",
        "diff -u",
        "jq",
        "REST 補驗",
        "N/A",
    ]

    missing = [value for value in task2_required if value not in row2_recheck]
    missing.extend(value for value in task3_required if value not in row3_recheck)
    assert not missing, "重驗欄缺少全 match 裁決所需值：" + ", ".join(missing)

    conclusion = _section(report, "## 四、結論")
    gap = _section(report, "## 五、缺口")

    assert "身分欄位" in conclusion
    assert "無任一 mismatch" in conclusion
    assert "結論不降級" in conclusion
    assert "閉環（僅及 v0.2.0）" in conclusion
    assert "## 五、缺口\n\n無。\n" in gap
    assert "未閉環" not in conclusion
    assert "結論降級" not in conclusion


def test_task4_does_not_touch_evidence_or_marker_lines():
    report = _read_report()

    expected_markers = [
        "### #1 線上 release body（來源：任務 #1 執行紀錄，2026-07-06）",
        "### #2 線上 body 結構斷言（來源：任務 #1 執行紀錄，2026-07-06）",
        "### #3 release-smoke 觸發（來源：任務 #2 執行紀錄，2026-07-06）",
        "run_id=27905531397",
        "gh run view 27905531397 --json databaseId,event,status,conclusion,workflowName,url",
        "gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'",
    ]
    for marker in expected_markers:
        assert marker in report

    diff = subprocess.run(
        ["git", "diff", "--quiet", "--", "docs/evidence"],
        cwd=ROOT,
        check=False,
    )
    assert diff.returncode == 0, "docs/evidence 不得有 git diff"

    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "docs/evidence"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    allowed_new_evidence = {"?? docs/evidence/token-rotation-2026-07-10.md"}
    unexpected = [
        line
        for line in status.stdout.splitlines()
        if line not in allowed_new_evidence
    ]
    assert unexpected == [], "docs/evidence 不得有非 token 輪替 evidence 變更：\n" + "\n".join(unexpected)


def test_task4_report_commands_do_not_use_bare_python_or_pytest():
    report = _read_report()

    bare_python = re.findall(r"(?<![\w/.-])python(?![3\w])", report)
    bare_pytest = re.findall(r"(?<![\w/.-])pytest(?![\w.-])", report)

    assert bare_python == []
    assert bare_pytest == []
