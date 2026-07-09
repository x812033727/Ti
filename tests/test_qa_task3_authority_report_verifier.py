from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from scripts.verify_authority_report_28933251516 import build_paths, main, run_checks

RUN_ID = "28933251516"
TEST_NAME = (
    "tests/test_qa_task2_retry_doc_retained.py::"
    "test_retry_doc_pytest_contract_passes_with_expected_count"
)
MESSAGE = "AssertionError: expected 2 passed, got 1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fixture(tmp_path: Path) -> Path:
    evidence_dir = tmp_path / ".ci-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "__pycache__").mkdir()
    (evidence_dir / "build_ci_authority_report_28933251516.py").write_text(
        "# helper\n", encoding="utf-8"
    )
    (evidence_dir / f"run-{RUN_ID}.json").write_text("{}", encoding="utf-8")

    raw_lines = [
        "noise before",
        f"test (3.11)\tpytest\t2026-07-08T09:47:26Z FAILED {TEST_NAME} - {MESSAGE}",
        "noise between",
        f"test (3.12)\tpytest\t2026-07-08T09:47:36Z FAILED {TEST_NAME} - {MESSAGE}",
    ]
    failed_lines = [raw_lines[1], raw_lines[3]]
    raw_log = evidence_dir / f"run-{RUN_ID}.log"
    failed_log = evidence_dir / f"run-{RUN_ID}.failed.log"
    raw_log.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
    failed_log.write_text("\n".join(failed_lines) + "\n", encoding="utf-8")

    report = evidence_dir / f"run-{RUN_ID}.authority-report.md"
    raw_digest = _sha256(raw_log)
    report.write_text(
        "\n".join(
            [
                f"# run {RUN_ID} authority report",
                "",
                "本檔為唯一權威。",
                f"原始 log sha256: `{raw_digest}`",
                "",
                "## 失敗三要素核對",
                "| job | test | 原始 log 行號 | failed.log 行號 | timestamp | message |",
                "|---|---|---:|---:|---|---|",
                f"| test (3.11) | `{TEST_NAME}` | 2 | 1 | 2026-07-08T09:47:26Z | `{MESSAGE}` |",
                f"| test (3.12) | `{TEST_NAME}` | 4 | 2 | 2026-07-08T09:47:36Z | `{MESSAGE}` |",
                "",
                "## 應忽略殘檔清單（非權威）",
                "| name | reason |",
                "|---|---|",
                "| `__pycache__` | helper cache |",
                "| `build_ci_authority_report_28933251516.py` | helper |",
                f"| `run-{RUN_ID}.failed.log` | evidence |",
                f"| `run-{RUN_ID}.json` | evidence |",
                f"| `run-{RUN_ID}.log` | evidence |",
                "",
                "## sha256",
                "本檔不內嵌自身 sha256。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sidecar = report.with_suffix(report.suffix + ".sha256")
    sidecar.write_text(f"{_sha256(report)}  {report.name}\n", encoding="utf-8")
    return evidence_dir


def test_verifier_accepts_complete_authority_report_fixture(tmp_path: Path) -> None:
    evidence_dir = _write_fixture(tmp_path)

    errors = run_checks(build_paths(evidence_dir), check_git=False)

    assert errors == []


def test_sidecar_is_checked_from_evidence_directory(tmp_path: Path) -> None:
    evidence_dir = _write_fixture(tmp_path)
    paths = build_paths(evidence_dir)

    result = subprocess.run(
        ["sha256sum", "-c", paths.sidecar.name],
        cwd=evidence_dir,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert result.returncode == 0
    assert f"{paths.report.name}: OK" in result.stdout


def test_cli_entrypoint_accepts_complete_fixture(tmp_path: Path) -> None:
    evidence_dir = _write_fixture(tmp_path)

    exit_code = main(["--evidence-dir", str(evidence_dir), "--skip-git-status"])

    assert exit_code == 0


def test_verifier_rejects_absolute_sidecar_target(tmp_path: Path) -> None:
    evidence_dir = _write_fixture(tmp_path)
    paths = build_paths(evidence_dir)
    paths.sidecar.write_text(f"{_sha256(paths.report)}  {paths.report}\n", encoding="utf-8")

    errors = run_checks(paths, check_git=False)

    assert any("sidecar target 必須是裸檔名" in error for error in errors)


def test_verifier_rejects_missing_residue_item(tmp_path: Path) -> None:
    evidence_dir = _write_fixture(tmp_path)
    paths = build_paths(evidence_dir)
    text = paths.report.read_text(encoding="utf-8")
    paths.report.write_text(
        text.replace(f"| `run-{RUN_ID}.json` | evidence |\n", ""),
        encoding="utf-8",
    )
    paths.sidecar.write_text(f"{_sha256(paths.report)}  {paths.report.name}\n", encoding="utf-8")

    errors = run_checks(paths, check_git=False)

    assert any(f"run-{RUN_ID}.json" in error for error in errors)
