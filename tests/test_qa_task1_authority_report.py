from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_DIR = Path("/opt/ti-autopilot-work/.ci-evidence")
RUN_ID = "28933251516"
REPORT = EVIDENCE_DIR / f"run-{RUN_ID}.authority-report.md"
SIDECAR = REPORT.with_suffix(REPORT.suffix + ".sha256")
RAW_LOG = EVIDENCE_DIR / f"run-{RUN_ID}.log"
FAILED_LOG = EVIDENCE_DIR / f"run-{RUN_ID}.failed.log"


@dataclass(frozen=True)
class FailureSummary:
    job: str
    timestamp: str
    test_name: str
    message: str
    line_number: int
    line: str


@dataclass(frozen=True)
class ReportFailure:
    job: str
    test_name: str
    raw_line_number: int
    failed_line_number: int
    timestamp: str
    message: str


FAILURE_RE = re.compile(
    r"^(?P<job>[^\t]+)\t[^\t]+\t(?P<timestamp>\S+) "
    r"FAILED (?P<test_name>\S+::\S+) - (?P<message>.+)$"
)
REPORT_FAILURE_RE = re.compile(
    r"^\| (?P<job>test \([^)]+\)) "
    r"\| `(?P<test_name>[^`]+)` "
    r"\| (?P<raw_line>\d+) "
    r"\| (?P<failed_line>\d+) "
    r"\| (?P<timestamp>[^|]+?) "
    r"\| `(?P<message>[^`]+)` \|$"
)
IGNORE_ROW_RE = re.compile(r"^\| `(?P<name>[^`]+)` \| (?P<reason>.+) \|$")


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_log_failures(path: Path) -> list[FailureSummary]:
    failures: list[FailureSummary] = []
    for line_number, line in enumerate(_read_lines(path), start=1):
        match = FAILURE_RE.match(line)
        if not match:
            continue
        failures.append(
            FailureSummary(
                job=match.group("job"),
                timestamp=match.group("timestamp"),
                test_name=match.group("test_name"),
                message=match.group("message"),
                line_number=line_number,
                line=line,
            )
        )
    return failures


def _extract_report_failures(report_text: str) -> list[ReportFailure]:
    failures: list[ReportFailure] = []
    for line in report_text.splitlines():
        match = REPORT_FAILURE_RE.match(line)
        if not match:
            continue
        failures.append(
            ReportFailure(
                job=match.group("job"),
                test_name=match.group("test_name"),
                raw_line_number=int(match.group("raw_line")),
                failed_line_number=int(match.group("failed_line")),
                timestamp=match.group("timestamp").strip(),
                message=match.group("message"),
            )
        )
    return failures


def _extract_ignored_items(report_text: str) -> set[str]:
    try:
        section = report_text.split("## 應忽略殘檔清單（非權威）", maxsplit=1)[1]
    except IndexError:
        return set()
    section = section.split("\n## ", maxsplit=1)[0]
    items: set[str] = set()
    for line in section.splitlines():
        match = IGNORE_ROW_RE.match(line)
        if match:
            items.add(match.group("name"))
    return items


def test_authority_report_exists_is_unique_and_declares_authority() -> None:
    assert REPORT.is_file()
    text = REPORT.read_text(encoding="utf-8")

    authority_reports = sorted(EVIDENCE_DIR.glob("*.authority-report.md"))
    assert authority_reports == [REPORT]
    assert "本檔為唯一權威" in text
    assert str(REPORT) in text


def test_failure_summary_matches_raw_log_and_failed_log_line_numbers() -> None:
    text = REPORT.read_text(encoding="utf-8")
    report_failures = _extract_report_failures(text)
    raw_failures = _extract_log_failures(RAW_LOG)
    failed_failures = _extract_log_failures(FAILED_LOG)

    assert report_failures, "report did not expose parseable failure summary rows"
    assert len(report_failures) == len(raw_failures) == len(failed_failures)

    expected = {
        (failure.job, failure.test_name, failure.message, failure.line_number)
        for failure in raw_failures
    }
    reported = {
        (failure.job, failure.test_name, failure.message, failure.raw_line_number)
        for failure in report_failures
    }
    assert reported == expected

    raw_lines = _read_lines(RAW_LOG)
    failed_lines = _read_lines(FAILED_LOG)
    for failure in report_failures:
        assert "::" in failure.test_name
        assert failure.message
        raw_line = raw_lines[failure.raw_line_number - 1]
        failed_line = failed_lines[failure.failed_line_number - 1]
        assert raw_line == failed_line
        assert failure.timestamp in raw_line
        assert f"FAILED {failure.test_name} - {failure.message}" in raw_line


def test_ignored_residue_list_matches_current_evidence_directory() -> None:
    text = REPORT.read_text(encoding="utf-8")
    ignored_items = _extract_ignored_items(text)

    actual_non_authority_items = {path.name for path in EVIDENCE_DIR.iterdir() if path != REPORT}

    assert REPORT.name not in ignored_items
    assert ignored_items == actual_non_authority_items


def test_log_and_report_sha256_are_file_external_and_recomputable() -> None:
    text = REPORT.read_text(encoding="utf-8")
    raw_log_hash = _sha256(RAW_LOG)
    report_hash = _sha256(REPORT)

    assert f"原始 log sha256: `{raw_log_hash}`" in text
    assert report_hash not in text

    sidecar_parts = SIDECAR.read_text(encoding="utf-8").strip().split()
    assert sidecar_parts[0] == report_hash
    assert Path(sidecar_parts[1]).name == REPORT.name


def test_acceptance_sha256sum_command_succeeds_from_qa_workdir() -> None:
    result = subprocess.run(
        ["sha256sum", "-c", str(SIDECAR)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{REPORT.name}: OK" in result.stdout


def test_evidence_git_status_is_clean() -> None:
    result = subprocess.run(
        ["git", "-C", str(EVIDENCE_DIR.parent), "status", "--short", ".ci-evidence"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
