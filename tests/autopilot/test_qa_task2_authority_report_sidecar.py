from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = Path("/opt/ti-autopilot-work/.ci-evidence")
RUN_ID = "28933251516"
REPORT = EVIDENCE_DIR / f"run-{RUN_ID}.authority-report.md"
SIDECAR = EVIDENCE_DIR / f"run-{RUN_ID}.authority-report.md.sha256"
SOURCE_LOG = EVIDENCE_DIR / f"run-{RUN_ID}.log"

SHA256_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sidecar() -> tuple[str, str]:
    text = SIDECAR.read_text(encoding="utf-8").strip()
    match = SHA256_LINE_RE.fullmatch(text)
    assert match is not None, f"sidecar is not sha256sum format: {text!r}"
    return match.group(1), match.group(2)


def run_sha256sum_check(cwd: Path, check_file: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sha256sum", "-c", str(check_file)],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_task2_required_artifacts_exist() -> None:
    assert REPORT.is_file(), f"missing authority report: {REPORT}"
    assert SIDECAR.is_file(), f"missing report sha256 sidecar: {SIDECAR}"
    assert SOURCE_LOG.is_file(), f"missing source log: {SOURCE_LOG}"


def test_sidecar_digest_is_for_report_not_source_log() -> None:
    sidecar_digest, sidecar_target = parse_sidecar()

    assert sidecar_digest == sha256_file(REPORT)
    assert sidecar_digest != sha256_file(SOURCE_LOG)
    assert Path(sidecar_target) == REPORT


def test_sidecar_can_be_checked_from_its_own_directory() -> None:
    completed = run_sha256sum_check(EVIDENCE_DIR, SIDECAR.name)

    assert completed.returncode == 0, completed.stdout
    assert "OK" in completed.stdout


def test_acceptance_command_passes_from_workspace_root() -> None:
    completed = run_sha256sum_check(WORKSPACE_ROOT, SIDECAR)

    assert completed.returncode == 0, (
        "驗收指令 `sha256sum -c /opt/ti-autopilot-work/.ci-evidence/"
        "run-28933251516.authority-report.md.sha256` 必須能從工作目錄直接通過。\n"
        f"cwd={WORKSPACE_ROOT}\n"
        f"output:\n{completed.stdout}"
    )
    assert "OK" in completed.stdout


def test_report_contains_source_log_digest_but_not_its_own_digest() -> None:
    report_text = REPORT.read_text(encoding="utf-8")
    report_digest = sha256_file(REPORT)
    source_log_digest = sha256_file(SOURCE_LOG)
    sidecar_digest, _sidecar_target = parse_sidecar()

    assert source_log_digest in report_text
    assert report_digest == sidecar_digest
    assert report_digest not in report_text
