from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "release-e2e-closure-report.md"
EVIDENCE_DIR = ROOT / "docs" / "evidence"
SHA256_RE = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{64})(?![0-9A-Fa-f])")


@dataclass(frozen=True)
class HashLiteral:
    ordinal: int
    line: int
    sha256: str


def _task2_path(name: str) -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / f"task2-qa-{name}"


def _scan_sha256_literals(text: str) -> list[HashLiteral]:
    found: list[HashLiteral] = []
    ordinal = 1
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in SHA256_RE.finditer(line):
            found.append(HashLiteral(ordinal=ordinal, line=line_number, sha256=match.group(1)))
            ordinal += 1
    return found


def _grep_evidence(sha256: str, evidence_files: list[Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["grep", "-H", "-n", "-F", "--", sha256, *map(str, evidence_files)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_sha256_scanner_has_strict_64_hex_boundaries():
    exact = "a" * 64
    uppercase = "B" * 64
    forty_hex_git_sha = "c" * 40
    too_long = "d" * 65

    scanned = _scan_sha256_literals(
        f"keep {exact}\n"
        f"keep uppercase {uppercase}\n"
        f"ignore git sha {forty_hex_git_sha}\n"
        f"ignore adjacent hex 0{exact}f\n"
        f"ignore too-long {too_long}\n"
    )

    assert [item.sha256 for item in scanned] == [exact, uppercase]
    assert [item.line for item in scanned] == [1, 2]


def test_report_sha256_literals_are_all_exact_evidence_values():
    report_text = REPORT.read_text(encoding="utf-8")
    evidence_files = sorted(EVIDENCE_DIR.glob("*.json"))
    literals = _scan_sha256_literals(report_text)
    assert literals, "報告內至少應有一個 sha256 字面值可供反查"
    assert evidence_files, "docs/evidence/*.json 不可為空"

    report_hash_lines = _task2_path("report-hash-lines.tsv")
    grep_raw = _task2_path("evidence-grep-raw.txt")
    grep_counts = _task2_path("evidence-grep-counts.tsv")
    missing_file = _task2_path("missing.tsv")
    summary_file = _task2_path("summary.json")

    report_hash_lines.write_text(
        "\n".join(f"{item.ordinal}\t{item.line}\t{item.sha256}" for item in literals) + "\n",
        encoding="utf-8",
    )

    backed_count = 0
    missing: list[HashLiteral] = []
    raw_chunks: list[str] = []
    count_lines: list[str] = []
    for item in literals:
        result = _grep_evidence(item.sha256, evidence_files)
        hits = [line for line in result.stdout.splitlines() if line]
        raw_chunks.append(
            f"## ordinal={item.ordinal} line={item.line} sha256={item.sha256}\n"
            + (result.stdout if result.stdout else "<NO MATCH>\n")
            + (result.stderr if result.stderr else "")
        )
        count_lines.append(f"{item.ordinal}\t{item.line}\t{item.sha256}\t{len(hits)}")
        if hits:
            backed_count += 1
        else:
            missing.append(item)

    grep_raw.write_text("\n".join(raw_chunks), encoding="utf-8")
    grep_counts.write_text("\n".join(count_lines) + "\n", encoding="utf-8")
    missing_file.write_text(
        "\n".join(f"{item.ordinal}\t{item.line}\t{item.sha256}" for item in missing),
        encoding="utf-8",
    )
    summary = {
        "report_sha256_literal_count": len(literals),
        "evidence_backed_literal_count": backed_count,
        "missing_count": len(missing),
        "unique_report_sha256_values": sorted({item.sha256 for item in literals}),
        "outputs": {
            "report_hash_lines": str(report_hash_lines),
            "evidence_grep_raw": str(grep_raw),
            "evidence_grep_counts": str(grep_counts),
            "missing": str(missing_file),
        },
    }
    summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert len(literals) == backed_count, (
        "報告內 sha256 字面值總數必須等於 evidence grep 反查命中數；"
        f"缺漏見 {missing_file}"
    )
    assert missing == []
