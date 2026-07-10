from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = ROOT / ".ci-evidence"
REPORT = EVIDENCE_DIR / "ci-failure-authority-report.md"
SIDECAR = EVIDENCE_DIR / "ci-failure-authority-report.md.sha256"
LOG = EVIDENCE_DIR / "run-28933251516.log"

AUTHORITY_PHRASE = "".join(("本檔為唯一", "權威"))
EXPECTED_LOG_SHA256 = "ddad1c874602e8730cb15fa7e6c5d9b5be0c6f404dde8936fa891fdfa2121663"
EXPECTED_LOG_LINES = 9817
EXPECTED_FAILURES = [
    (
        5423,
        "tests/test_qa_task2_retry_doc_retained.py::"
        "test_retry_doc_pytest_contract_passes_with_expected_count",
        "AssertionError: .venv/bin/python is required by the acceptance command",
    ),
    (
        9794,
        "tests/test_qa_task2_retry_doc_retained.py::"
        "test_retry_doc_pytest_contract_passes_with_expected_count",
        "AssertionError: .venv/bin/python is required by the acceptance command",
    ),
]


def _run(args: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_task3_ci_evidence_authority_acceptance_contract() -> None:
    failures: list[str] = []

    if not EVIDENCE_DIR.is_dir():
        raise AssertionError(
            "缺少 .ci-evidence/ 目錄，無法驗收 canonical 報告、原始 log 與 sidecar"
        )

    entries = sorted(path.name for path in EVIDENCE_DIR.iterdir())

    if not REPORT.is_file():
        failures.append("缺少 canonical 報告: .ci-evidence/ci-failure-authority-report.md")
        report_text = ""
    else:
        report_text = REPORT.read_text(encoding="utf-8")

    if not SIDECAR.is_file():
        failures.append("缺少 sidecar: .ci-evidence/ci-failure-authority-report.md.sha256")

    if not LOG.is_file():
        failures.append("缺少原始 log: .ci-evidence/run-28933251516.log")
    else:
        log_bytes = LOG.read_bytes()
        actual_sha = hashlib.sha256(log_bytes).hexdigest()
        if actual_sha != EXPECTED_LOG_SHA256:
            failures.append(f"原始 log sha256 不符: actual={actual_sha}")

        actual_line_count = len(log_bytes.decode("utf-8", errors="replace").splitlines())
        if actual_line_count != EXPECTED_LOG_LINES:
            failures.append(f"原始 log 行數不符: actual={actual_line_count}")

    if report_text:
        if EXPECTED_LOG_SHA256 not in report_text:
            failures.append("報告未內嵌原始 log sha256")
        if str(EXPECTED_LOG_LINES) not in report_text:
            failures.append("報告未內嵌原始 log wc -l 行數")

        missing_residuals = [
            name for name in entries if name != REPORT.name and name not in report_text
        ]
        if missing_residuals:
            failures.append("應忽略殘檔清單漏項: " + ", ".join(missing_residuals))

    grep_result = _run(["grep", "-rl", AUTHORITY_PHRASE, ".ci-evidence"])
    authority_hits = sorted(
        line.strip() for line in grep_result.stdout.splitlines() if line.strip()
    )
    if authority_hits != [".ci-evidence/ci-failure-authority-report.md"]:
        failures.append(
            "權威字樣命中應只有 canonical 報告；"
            f"actual={authority_hits!r}, grep_rc={grep_result.returncode}"
        )

    for line_no, test_name, message in EXPECTED_FAILURES:
        if report_text:
            for token in (str(line_no), test_name, message):
                if token not in report_text:
                    failures.append(f"報告缺少三要素 token: line={line_no}, token={token}")

        if LOG.is_file():
            sed_result = _run(["sed", "-n", f"{line_no}p", str(LOG)])
            expected_fragment = f"FAILED {test_name} - {message}"
            if sed_result.returncode != 0 or expected_fragment not in sed_result.stdout:
                failures.append(
                    f"sed 回指不符: line={line_no}, rc={sed_result.returncode}, "
                    f"stdout={sed_result.stdout.strip()!r}"
                )

    if SIDECAR.is_file():
        sha_result = _run(["sha256sum", "-c", SIDECAR.name], cwd=EVIDENCE_DIR)
        if sha_result.returncode != 0:
            failures.append("sidecar sha256sum -c 未通過: " + sha_result.stdout.strip())

    assert not failures, "\n".join(failures)
