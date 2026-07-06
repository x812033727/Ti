from __future__ import annotations

import hashlib
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


def _expected_hashes(evidence_files: list[Path]) -> set[str]:
    values: set[str] = set()
    for path in evidence_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        body_sha = data.get("body_sha256")
        if isinstance(body_sha, str):
            values.add(body_sha)

        for key in ("gh_release_view", "rest_release_by_tag_subset"):
            section = data.get(key)
            body = section.get("body") if isinstance(section, dict) else None
            if isinstance(body, str):
                values.add(hashlib.sha256(body.encode("utf-8")).hexdigest())
                values.add(hashlib.sha256((body + "\n").encode("utf-8")).hexdigest())
    return values


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
    missing_file = _task2_path("missing.tsv")
    summary_file = _task2_path("summary.json")

    report_hash_lines.write_text(
        "\n".join(f"{item.ordinal}\t{item.line}\t{item.sha256}" for item in literals) + "\n",
        encoding="utf-8",
    )

    backed_count = 0
    missing: list[HashLiteral] = []
    expected_hashes = _expected_hashes(evidence_files)
    for item in literals:
        if item.sha256 in expected_hashes:
            backed_count += 1
        else:
            missing.append(item)

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
            "missing": str(missing_file),
        },
    }
    summary_file.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    assert len(literals) == backed_count, (
        f"報告內 sha256 字面值總數必須等於預期白名單命中數；缺漏見 {missing_file}"
    )
    assert missing == []
