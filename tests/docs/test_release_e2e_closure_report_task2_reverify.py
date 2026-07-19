from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "release-e2e-closure-report.md"
EVIDENCE = ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json"
TABLE_HEADER = "| # | 閉環環節 | Evidence 檔路徑 | 原 `captured_at_utc` | 關鍵勾稽值 | 本次線上重驗 | 雜湊 / 判定規則 |"


def _table_rows(report: str) -> list[str]:
    lines = report.splitlines()
    start = lines.index(TABLE_HEADER)
    rows: list[str] = []
    for line in lines[start + 2 :]:
        if not line.startswith("| #"):
            break
        rows.append(line)
    return rows


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip("|").split("|")]


def _task2_reverify_cell() -> str:
    rows = _table_rows(REPORT.read_text(encoding="utf-8"))
    for row in rows:
        cells = _cells(row)
        if cells[0] == "#2":
            return cells[5]
    raise AssertionError("三列表找不到 #2 列")


def _task2_inline_command() -> str:
    cell = _task2_reverify_cell()
    marker = "自足重驗/比對指令：`"
    start = cell.index(marker) + len(marker)
    end = cell.index("`", start)
    return cell[start:end]


def test_task2_row_has_20260707_key_values_and_copyable_compare_command():
    cell = _task2_reverify_cell()
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    required_fragments = [
        "2026-07-07 PASS",
        f"verdict={evidence['verdict']}",
        "problems=[]",
        "timeout 60 env PYTHONPATH=. python3 scripts/check_release_body_structure.py",
        "jq -S '{verdict, checks, problems}'",
        "diff -u",
        "docs/evidence/release-v0.2.0-body-structure-verdict.json",
    ]
    for fragment in required_fragments:
        assert fragment in cell

    for key, value in evidence["checks"].items():
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        assert f"{key}={rendered}" in cell

    assert "$TMPDIR" not in cell
    assert "/tmp/" not in cell
    assert "task2" not in cell.lower()
    assert not re.search(r"(?<![\w./-])python(?![\w./-])", cell)
    assert not re.search(r"(?<![\w./-])pytest(?:\s|$)", cell)


def test_task2_report_inline_command_is_copyable_and_exits_zero():
    result = subprocess.run(
        ["bash", "-lc", _task2_inline_command()],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert (
        result.returncode == 0
    ), f"報告 #2 欄內可照抄指令執行失敗\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.stdout == ""


def test_task2_body_structure_reverify_command_matches_evidence_with_jq_diff():
    tmpdir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    env = os.environ.copy()
    env["TMPDIR"] = str(tmpdir)
    command = r"""
set -euo pipefail
: "${TMPDIR:?TMPDIR must be set}"
check_log="$TMPDIR/task2_qa_body_structure_check.log"
expected="$TMPDIR/task2_qa_body_structure_expected.json"
actual="$TMPDIR/task2_qa_body_structure_actual.json"
diff_out="$TMPDIR/task2_qa_body_structure_diff.txt"

timeout 60 env PYTHONPATH=. python3 scripts/check_release_body_structure.py >"$check_log"
jq -S '{verdict, checks, problems}' docs/evidence/release-v0.2.0-body-structure-verdict.json >"$expected"
PYTHONPATH=. python3 - <<'PY' >"$actual"
import json
from pathlib import Path

from scripts import check_release_body_structure as s
from studio.release_note import BREAKING_HEADING

evidence = json.loads(
    Path("docs/evidence/release-v0.2.0-online-body.json").read_text(encoding="utf-8")
)
version = s.pyproject_version()
problems = s.check(evidence, version)
gh = s.normalize(evidence["gh_release_view"]["body"])
rest = s.normalize(evidence["rest_release_by_tag_subset"]["body"])
first_h2 = s.first_top_level_h2(gh)
lower_body = gh.lower()
checks = {
    "雙來源正規化後逐字相等(gh vs REST)": gh == rest,
    "頂部第一個頂層## 區塊": first_h2,
    "頂部即 Breaking 置頂": first_h2 == BREAKING_HEADING,
    "四要素齊(①行為變動②原因③before/after④生效版本)": all(
        anchor in gh and any(keyword.lower() in lower_body for keyword in semantics)
        for _, anchor, semantics in s.FOUR_ELEMENTS
    ),
    "生效版本逐字對應_自0.2.0起": (
        f"自 `{version}` 起" in gh or f"自 {version} 起" in gh
    ),
    "逃生艙_TI_REQUIRE_CHOWN=warn/off": (
        "TI_REQUIRE_CHOWN=warn" in gh and "TI_REQUIRE_CHOWN=off" in gh
    ),
}
payload = {
    "verdict": "PASS" if not problems else "FAIL",
    "checks": checks,
    "problems": problems,
}
print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
PY
diff -u "$expected" "$actual" >"$diff_out"
printf 'task2_check_log=%s\n' "$check_log"
printf 'task2_expected=%s\n' "$expected"
printf 'task2_actual=%s\n' "$actual"
printf 'task2_diff=%s\n' "$diff_out"
"""
    result = subprocess.run(
        ["bash", "-lc", command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    diff_out = tmpdir / "task2_qa_body_structure_diff.txt"
    if result.returncode != 0:
        diff_text = diff_out.read_text(encoding="utf-8") if diff_out.exists() else "<no diff>"
        raise AssertionError(
            "task2 jq+diff 重驗失敗\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n"
            f"diff:\n{diff_text}"
        )

    output_paths = [
        Path(line.split("=", 1)[1])
        for line in result.stdout.splitlines()
        if line.startswith("task2_")
    ]
    assert len(output_paths) == 4
    assert all(path.name.startswith("task2_qa_") for path in output_paths)
    assert diff_out.exists()
    assert diff_out.read_text(encoding="utf-8") == ""
    for name in (
        "task2_qa_body_structure_check.log",
        "task2_qa_body_structure_expected.json",
        "task2_qa_body_structure_actual.json",
        "task2_qa_body_structure_diff.txt",
    ):
        assert (tmpdir / name).exists()
