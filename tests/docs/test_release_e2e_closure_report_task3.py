from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "release-e2e-closure-report.md"
EVIDENCE_DIR = ROOT / "docs" / "evidence"


def _read_report() -> str:
    return REPORT.read_text(encoding="utf-8")


def _load_json(name: str) -> dict:
    return json.loads((EVIDENCE_DIR / name).read_text(encoding="utf-8"))


def _table_rows(report: str) -> list[str]:
    lines = report.splitlines()
    start = lines.index(
        "| # | 閉環環節 | Evidence 檔路徑 | 原 `captured_at_utc` | 關鍵勾稽值 | 本次線上重驗 | 雜湊 / 判定規則 |"
    )
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("| #"):
            break
        rows.append(line)
    return rows


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip("|").split("|")]


def _section(report: str, heading: str) -> str:
    start = report.index(heading)
    next_heading = report.find("\n## ", start + len(heading))
    return report[start:] if next_heading == -1 else report[start:next_heading]


def test_three_row_table_has_required_evidence_values_and_20260706_result():
    report = _read_report()
    rows = _table_rows(report)
    assert len(rows) == 3

    online = _load_json("release-v0.2.0-online-body.json")
    verdict = _load_json("release-v0.2.0-body-structure-verdict.json")
    smoke = _load_json("release-smoke-v0.2.0-trigger.json")

    expectations = [
        (
            "docs/evidence/release-v0.2.0-online-body.json",
            online["captured_at_utc"],
            [
                f"body_sha256={online['body_sha256']}",
                f"body_match={str(online['body_match']).lower()}",
                f"tag_match={str(online['tag_match']).lower()}",
                f"url_match={str(online['url_match']).lower()}",
            ],
        ),
        (
            "docs/evidence/release-v0.2.0-body-structure-verdict.json",
            verdict["captured_at_utc"],
            [
                f"verdict={verdict['verdict']}",
                "problems=[]",
                "四要素齊=true",
                "逃生艙_TI_REQUIRE_CHOWN=warn/off=true",
            ],
        ),
        (
            "docs/evidence/release-smoke-v0.2.0-trigger.json",
            smoke["captured_at_utc"],
            [
                f"run_id={smoke['run_id']}",
                f"event={smoke['event']}",
                f"status={smoke['status']}",
                f"conclusion={smoke['conclusion']}",
                f"workflow_path={smoke['workflow_path']}",
            ],
        ),
    ]

    missing_dates = []
    for row, (evidence_path, captured_at, required_values) in zip(rows, expectations, strict=True):
        cells = _cells(row)
        assert evidence_path in cells[2]
        assert captured_at in cells[3]
        for value in required_values:
            assert value in cells[4]
        if "2026-07-06" not in cells[5]:
            missing_dates.append(row)
        assert cells[6], f"雜湊 / 判定規則欄不得空白：{row}"
    assert not missing_dates, "本次線上重驗欄缺日期：\n" + "\n".join(missing_dates)


def test_conclusion_is_limited_to_v020():
    report = _read_report()
    conclusion = _section(report, "## 四、結論")

    assert "閉環（僅及 v0.2.0）" in conclusion
    assert "v* tag-push" not in conclusion


def test_report_has_no_stale_untracked_delivery_text():
    report = _read_report()

    assert "?? docs/release-e2e-closure-report.md" not in report


def test_report_keeps_na_path_rule_and_does_not_introduce_new_hash_values():
    report = _read_report()
    online = _load_json("release-v0.2.0-online-body.json")
    # 2026-07-06 重驗 exact body hash：由 evidence 內存 body 重算導出（非硬編碼放行）。
    # evidence 的 body_sha256 為 jq -r 含結尾換行的 hash（計算方式瑕疵，修復列移交待辦）。
    reverify_exact = hashlib.sha256(
        online["gh_release_view"]["body"].encode("utf-8")
    ).hexdigest()
    hashes = set(re.findall(r"\b[a-f0-9]{64}\b", report))

    assert hashes == {online["body_sha256"], reverify_exact}
    assert "gh run view --json` 為 `N/A`" in report or "gh run view `path` | N/A" in report
    assert "REST 補驗" in report
