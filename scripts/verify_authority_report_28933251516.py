#!/usr/bin/env python3
"""Verify the run-28933251516 CI authority report artifacts."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RUN_ID = "28933251516"
DEFAULT_EVIDENCE_DIR = Path(__file__).resolve().parents[1] / ".ci-evidence"

SHA256_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")
LOG_FAILURE_RE = re.compile(
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
IGNORE_SECTION_TITLE = "## 應忽略殘檔清單（非權威）"


@dataclass(frozen=True)
class EvidencePaths:
    evidence_dir: Path
    report: Path
    sidecar: Path
    raw_log: Path
    failed_log: Path


@dataclass(frozen=True)
class LogFailure:
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


def build_paths(evidence_dir: Path) -> EvidencePaths:
    report = evidence_dir / f"run-{RUN_ID}.authority-report.md"
    return EvidencePaths(
        evidence_dir=evidence_dir,
        report=report,
        sidecar=report.with_suffix(report.suffix + ".sha256"),
        raw_log=evidence_dir / f"run-{RUN_ID}.log",
        failed_log=evidence_dir / f"run-{RUN_ID}.failed.log",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def extract_log_failures(path: Path) -> list[LogFailure]:
    failures: list[LogFailure] = []
    for line_number, line in enumerate(read_lines(path), start=1):
        match = LOG_FAILURE_RE.match(line)
        if not match:
            continue
        failures.append(
            LogFailure(
                job=match.group("job"),
                timestamp=match.group("timestamp"),
                test_name=match.group("test_name"),
                message=match.group("message"),
                line_number=line_number,
                line=line,
            )
        )
    return failures


def extract_report_failures(report_text: str) -> list[ReportFailure]:
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


def extract_ignored_items(report_text: str) -> set[str]:
    if IGNORE_SECTION_TITLE not in report_text:
        return set()
    section = report_text.split(IGNORE_SECTION_TITLE, maxsplit=1)[1]
    section = section.split("\n## ", maxsplit=1)[0]
    return {
        match.group("name") for line in section.splitlines() if (match := IGNORE_ROW_RE.match(line))
    }


def parse_sidecar(sidecar: Path) -> tuple[str, str]:
    text = sidecar.read_text(encoding="utf-8").strip()
    match = SHA256_LINE_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"sidecar 不是 sha256sum 格式: {text!r}")
    return match.group(1), match.group(2)


def _line_at(lines: list[str], line_number: int, label: str, errors: list[str]) -> str | None:
    if line_number < 1 or line_number > len(lines):
        errors.append(f"{label} 行號超出範圍: {line_number}")
        return None
    return lines[line_number - 1]


def check_required_files(paths: EvidencePaths, errors: list[str]) -> None:
    required = [paths.evidence_dir, paths.report, paths.sidecar, paths.raw_log, paths.failed_log]
    for path in required:
        if not path.exists():
            errors.append(f"缺少必要檔案: {path}")


def check_authority_statement(paths: EvidencePaths, report_text: str, errors: list[str]) -> None:
    authority_reports = sorted(paths.evidence_dir.glob("*.authority-report.md"))
    if authority_reports != [paths.report]:
        listed = ", ".join(path.name for path in authority_reports) or "<none>"
        errors.append(f"權威報告不是唯一一份: {listed}")
    if "本檔為唯一權威" not in report_text:
        errors.append("報告缺少「本檔為唯一權威」聲明")


def check_failures(paths: EvidencePaths, report_text: str, errors: list[str]) -> None:
    report_failures = extract_report_failures(report_text)
    raw_failures = extract_log_failures(paths.raw_log)
    failed_failures = extract_log_failures(paths.failed_log)

    if not report_failures:
        errors.append("報告沒有可解析的失敗三要素表格列")
        return
    if len(report_failures) != len(raw_failures) or len(report_failures) != len(failed_failures):
        errors.append(
            "失敗筆數不一致: "
            f"report={len(report_failures)} raw={len(raw_failures)} failed={len(failed_failures)}"
        )

    expected = {
        (failure.job, failure.test_name, failure.message, failure.line_number)
        for failure in raw_failures
    }
    reported = {
        (failure.job, failure.test_name, failure.message, failure.raw_line_number)
        for failure in report_failures
    }
    if reported != expected:
        errors.append("報告失敗清單與原始 log 的測試名/訊息/行號不一致")

    raw_lines = read_lines(paths.raw_log)
    failed_lines = read_lines(paths.failed_log)
    for failure in report_failures:
        if "::" not in failure.test_name:
            errors.append(f"測試名缺少 ::：{failure.test_name}")
        if not failure.message:
            errors.append(f"失敗訊息為空：{failure.test_name}")

        raw_line = _line_at(raw_lines, failure.raw_line_number, "原始 log", errors)
        failed_line = _line_at(failed_lines, failure.failed_line_number, "failed.log", errors)
        if raw_line is None or failed_line is None:
            continue
        if raw_line != failed_line:
            errors.append(
                "原始 log 與 failed.log 指定行內容不同: "
                f"{failure.raw_line_number} != {failure.failed_line_number}"
            )
        if failure.timestamp not in raw_line:
            errors.append(f"原始 log 行缺少 timestamp: {failure.timestamp}")
        expected_fragment = f"FAILED {failure.test_name} - {failure.message}"
        if expected_fragment not in raw_line:
            errors.append(f"原始 log 行缺少失敗片段: {expected_fragment}")


def check_residue_list(paths: EvidencePaths, report_text: str, errors: list[str]) -> None:
    ignored_items = extract_ignored_items(report_text)
    expected_items = {
        path.name
        for path in paths.evidence_dir.iterdir()
        if path not in {paths.report, paths.sidecar}
    }
    missing = sorted(expected_items - ignored_items)
    extra = sorted(ignored_items - expected_items)
    if missing or extra:
        errors.append(f"殘檔清單不吻合: missing={missing} extra={extra}")
    if paths.report.name in ignored_items:
        errors.append("殘檔清單誤列權威報告本體")
    if paths.sidecar.name in ignored_items:
        errors.append("殘檔清單誤列 sidecar；sidecar 另由 sha256 閘門驗證")


def check_sha256(paths: EvidencePaths, report_text: str, errors: list[str]) -> None:
    raw_digest = sha256_file(paths.raw_log)
    report_digest = sha256_file(paths.report)
    try:
        sidecar_digest, sidecar_target = parse_sidecar(paths.sidecar)
    except ValueError as exc:
        errors.append(str(exc))
        return

    if raw_digest not in report_text:
        errors.append(f"報告未包含來源 log sha256: {raw_digest}")
    if report_digest in report_text:
        errors.append("報告內嵌了自身 sha256，造成自參照風險")
    if sidecar_digest != report_digest:
        errors.append("sidecar sha256 與報告重算值不一致")
    if sidecar_target != paths.report.name:
        errors.append(f"sidecar target 必須是裸檔名 {paths.report.name!r}: got {sidecar_target!r}")

    result = subprocess.run(
        ["sha256sum", "-c", paths.sidecar.name],
        cwd=paths.evidence_dir,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        errors.append("sha256sum -c 未通過:\n" + result.stdout.strip())


def check_git_status(paths: EvidencePaths, errors: list[str]) -> None:
    result = subprocess.run(
        ["git", "-C", str(paths.evidence_dir.parent), "status", "--short", ".ci-evidence"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        errors.append("git status .ci-evidence 執行失敗:\n" + result.stderr.strip())
    elif result.stdout:
        errors.append("git status .ci-evidence 非乾淨:\n" + result.stdout.rstrip())


def run_checks(paths: EvidencePaths, *, check_git: bool = True) -> list[str]:
    errors: list[str] = []
    check_required_files(paths, errors)
    if errors:
        return errors

    report_text = paths.report.read_text(encoding="utf-8")
    check_authority_statement(paths, report_text, errors)
    check_failures(paths, report_text, errors)
    check_residue_list(paths, report_text, errors)
    check_sha256(paths, report_text, errors)
    if check_git:
        check_git_status(paths, errors)
    return errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="default: %(default)s",
    )
    parser.add_argument(
        "--skip-git-status",
        action="store_true",
        help="fixture/self-test only; production QA should leave this off",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = build_paths(args.evidence_dir.resolve())
    errors = run_checks(paths, check_git=not args.skip_git_status)
    if errors:
        print("FAIL: authority report QA validation failed")
        for error in errors:
            print(f"- {error}")
        return 1

    print("PASS: authority report QA validation passed")
    print(f"- evidence_dir: {paths.evidence_dir}")
    print(f"- report: {paths.report.name}")
    print(f"- sidecar: {paths.sidecar.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
