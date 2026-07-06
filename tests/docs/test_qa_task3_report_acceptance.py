from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "release-e2e-closure-report.md"
ONLINE_EVIDENCE = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"
STRUCTURE_VERDICT = ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json"


def _report_text() -> str:
    return REPORT.read_text(encoding="utf-8")


def _bash_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    in_block = False
    start_line = 0
    current: list[str] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip() == "```bash" and not in_block:
            in_block = True
            start_line = line_number + 1
            current = []
            continue
        if line.strip() == "```" and in_block:
            blocks.append((start_line, "\n".join(current) + "\n"))
            in_block = False
            continue
        if in_block:
            current.append(line)

    return blocks


def _bash_block_after(text: str, marker: str) -> str:
    marker_pos = text.index(marker)
    for start_line, block in _bash_blocks(text):
        prefix = "\n".join(text.splitlines()[: start_line - 1])
        if len(prefix) >= marker_pos:
            return block
    raise AssertionError(f"找不到 marker 後的 bash code block: {marker}")


def test_task3_report_rows_mark_capture_date_and_revalidation_status():
    report = _report_text()
    rows = [
        line
        for line in report.splitlines()
        if line.startswith("| #1 |") or line.startswith("| #2 |") or line.startswith("| #3 |")
    ]

    assert len(rows) == 3
    for row in rows:
        assert "擷取日期 2026-07-06" in row
        assert "本次線上重驗" in row


def test_task3_mismatch_forces_gap_section_and_degraded_limited_conclusion():
    report = _report_text()
    conclusion_start = report.index("## 四、結論")
    gap_start = report.index("## 五、缺口")
    conclusion = report[conclusion_start:gap_start]
    gap = report[gap_start:]

    assert "body_sha256" in report
    assert "mismatch" in report.lower()
    assert "降級" in conclusion
    assert "閉環（僅及 v0.2.0）" in conclusion
    assert "缺口 1" in gap
    assert "不修、不動 evidence 檔" in gap


def test_report_bash_blocks_are_shell_syntax_valid():
    for start_line, block in _bash_blocks(_report_text()):
        result = subprocess.run(
            ["bash", "-n"],
            input=block,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"bash block starting at line {start_line} is not syntax-valid\n"
            f"stderr:\n{result.stderr}\nblock:\n{block}"
        )


def test_task1_recorded_copy_paste_recheck_block_replays_against_evidence(
    tmp_path: Path,
):
    """可重跑指令不能只語法正確；用 evidence 派生 raw 檔也應完成自身 diff。"""
    evidence_before = ONLINE_EVIDENCE.read_bytes()
    evidence = json.loads(evidence_before.decode("utf-8"))

    (tmp_path / "task1-gh-release-view-v0.2.0-20260706T152100Z.json").write_text(
        json.dumps(evidence["gh_release_view"], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "task1-gh-api-release-v0.2.0-20260706T152100Z.json").write_text(
        json.dumps(evidence["rest_release_by_tag_subset"], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    block = _bash_block_after(_report_text(), "可重跑對帳指令（task1）")
    result = subprocess.run(
        ["bash", "-c", block],
        cwd=ROOT,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert ONLINE_EVIDENCE.read_bytes() == evidence_before
    assert result.returncode == 0, (
        "報告標成可重跑的 task1 對帳指令未能完成；"
        "這違反『比對指令可照抄重跑』。\n"
        f"returncode={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_task3_online_recheck_block_reports_only_body_hash_mismatch_with_stubbed_network(
    tmp_path: Path,
):
    """後段線上重驗指令應可照抄；diff 只能揭露已落字的 body_sha256 mismatch。"""
    evidence_before = ONLINE_EVIDENCE.read_bytes()
    evidence = json.loads(evidence_before.decode("utf-8"))

    gh_raw = tmp_path / "qa-gh-release.json"
    rest_raw = tmp_path / "qa-rest-release.json"
    gh_raw.write_text(
        json.dumps(evidence["gh_release_view"], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rest_raw.write_text(
        json.dumps(evidence["rest_release_by_tag_subset"], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    gh_stub = tmp_path / "gh"
    gh_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "auth" && "$2" == "status" ]]; then
  exit 0
fi
if [[ "$1" == "release" && "$2" == "view" && "$3" == "v0.2.0" ]]; then
  if [[ "$*" == *"--json tagName,url"* ]]; then
    printf '{"tagName":"v0.2.0","url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0"}\\n'
    exit 0
  fi
  if [[ "$*" == *"--json body,tagName,url"* ]]; then
    cat "$QA_GH_RAW"
    exit 0
  fi
fi
if [[ "$1" == "api" && "$2" == "repos/x812033727/Ti/releases/tags/v0.2.0" ]]; then
  cat "$QA_REST_RAW"
  exit 0
fi
printf 'unexpected gh invocation: %s\\n' "$*" >&2
exit 2
""",
        encoding="utf-8",
    )
    curl_stub = tmp_path / "curl"
    curl_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == "-sf https://api.github.com/rate_limit" ]]; then
  printf '{}\\n'
  exit 0
fi
printf 'unexpected curl invocation: %s\\n' "$*" >&2
exit 2
""",
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)
    curl_stub.chmod(0o755)

    block = _bash_block_after(
        _report_text(),
        "#1 gh CLI + REST 雙來源 raw 落檔與 identity 欄位比對",
    )
    result = subprocess.run(
        ["bash", "-c", block],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "QA_GH_RAW": str(gh_raw),
            "QA_REST_RAW": str(rest_raw),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert ONLINE_EVIDENCE.read_bytes() == evidence_before
    assert result.returncode == 0, (
        "報告後段線上重驗指令不可照抄執行。\n"
        f"returncode={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    exact_hash = hashlib.sha256(evidence["gh_release_view"]["body"].encode()).hexdigest()
    newline_hash = hashlib.sha256(
        f"{evidence['gh_release_view']['body']}\n".encode()
    ).hexdigest()
    changed_lines = [
        line
        for line in result.stdout.splitlines()
        if line.startswith(("+  ", "-  "))
    ]

    assert newline_hash == evidence["body_sha256"]
    assert f'"body_sha256": "{newline_hash}"' in result.stdout
    assert f'"body_sha256": "{exact_hash}"' in result.stdout
    assert any('"body_sha256"' in line for line in changed_lines)
    assert all('"tagName"' not in line and '"url"' not in line for line in changed_lines)


def test_task2_checker_recheck_matches_verdict_hash_fields():
    result = subprocess.run(
        ["timeout", "60", "env", "PYTHONPATH=.", "python3", "scripts/check_release_body_structure.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        "task2 checker 補驗應 exit 0。\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "核對通過" in result.stdout

    online = json.loads(ONLINE_EVIDENCE.read_text(encoding="utf-8"))
    verdict = json.loads(STRUCTURE_VERDICT.read_text(encoding="utf-8"))
    body = online["gh_release_view"]["body"]
    exact_hash = hashlib.sha256(body.encode()).hexdigest()
    newline_hash = hashlib.sha256(f"{body}\n".encode()).hexdigest()

    assert verdict["verdict"] == "PASS"
    assert verdict["problems"] == []
    assert all(verdict["checks"].values())
    assert verdict["body_sha256"] == online["body_sha256"]
    assert verdict["body_sha256_exact"] == exact_hash
    assert verdict["body_sha256_with_newline"] == newline_hash
