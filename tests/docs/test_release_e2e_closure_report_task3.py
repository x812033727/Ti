import json
import re
import shutil
import subprocess
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
    start = lines.index("| # | 閉環環節 | Evidence 檔路徑 | 原 `captured_at_utc` | 關鍵勾稽值 | 本次線上重驗 | 雜湊 / 判定規則 |")
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


def _between(report: str, start_heading: str, end_heading: str) -> str:
    start = report.index(start_heading)
    end = report.index(end_heading, start + len(start_heading))
    return report[start:end]


def _fenced_block_after(report: str, marker: str, language: str) -> str:
    marker_index = report.index(marker)
    fence = f"```{language}\n"
    block_start = report.index(fence, marker_index) + len(fence)
    block_end = report.index("\n```", block_start)
    return report[block_start:block_end]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


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
    for row, (evidence_path, captured_at, required_values) in zip(rows, expectations):
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
    hashes = set(re.findall(r"\b[a-f0-9]{64}\b", report))

    assert hashes == {online["body_sha256"]}
    assert "gh run view --json` 為 `N/A`" in report or "gh run view `path` | N/A" in report
    assert "REST 補驗" in report


def test_transcribed_online_outputs_match_current_commands_exactly():
    if shutil.which("gh") is None:
        raise AssertionError("gh CLI 不存在，無法驗證第二章線上轉錄")

    report = _between(
        _read_report(),
        "## 二、任務 #1／#2／#3 執行紀錄轉錄",
        "## 三、雜湊計算規則",
    )

    command_cases = [
        (
            "timeout 60 gh release view v0.2.0 --json body,tagName,url",
            ["gh", "release", "view", "v0.2.0", "--json", "body,tagName,url"],
            "json",
            "stdout",
        ),
        (
            "timeout 60 gh api repos/x812033727/Ti/releases/tags/v0.2.0 --jq '{body,tag_name,html_url,id,created_at,published_at}'",
            [
                "gh",
                "api",
                "repos/x812033727/Ti/releases/tags/v0.2.0",
                "--jq",
                "{body,tag_name,html_url,id,created_at,published_at}",
            ],
            "json",
            "stdout",
        ),
        (
            "timeout 60 env PYTHONPATH=. python3 scripts/check_release_body_structure.py",
            ["env", "PYTHONPATH=.", "python3", "scripts/check_release_body_structure.py"],
            "text",
            "stdout",
        ),
        (
            "gh run view 27905531397 --json event,status,conclusion,workflowName,path,url",
            [
                "gh",
                "run",
                "view",
                "27905531397",
                "--json",
                "event,status,conclusion,workflowName,path,url",
            ],
            "text",
            "stderr",
        ),
        (
            "gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{event,status,conclusion,name,path,html_url}'",
            [
                "gh",
                "api",
                "repos/x812033727/Ti/actions/runs/27905531397",
                "--jq",
                "{event,status,conclusion,name,path,html_url}",
            ],
            "json",
            "stdout",
        ),
        (
            "gh run view 27905531397 --json databaseId,event,status,conclusion,workflowName,url",
            [
                "gh",
                "run",
                "view",
                "27905531397",
                "--json",
                "databaseId,event,status,conclusion,workflowName,url",
            ],
            "json",
            "stdout",
        ),
        (
            "gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'",
            [
                "gh",
                "api",
                "repos/x812033727/Ti/actions/runs/27905531397",
                "--jq",
                "{id,event,status,conclusion,html_url,name,path}",
            ],
            "json",
            "stdout",
        ),
    ]

    for marker, command, language, stream_name in command_cases:
        completed = _run(command)
        stream = completed.stdout if stream_name == "stdout" else completed.stderr
        assert completed.returncode == (1 if "workflowName,path" in marker else 0)
        assert _fenced_block_after(report, marker, language) == stream.rstrip("\n")
