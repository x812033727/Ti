"""QA guard for task #2: retry-doc test must remain valid in place."""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = pathlib.Path("tests/test_task1_retry_doc.py")


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_retry_doc_test_is_tracked_in_place_and_clean():
    target = ROOT / TARGET

    assert target.is_file(), "tests/test_task1_retry_doc.py must exist in place"
    assert run_git("ls-files", str(TARGET)) == str(TARGET)
    assert run_git("status", "--short", "--", str(TARGET)) == ""

    tracked_target = run_git("ls-files", "*test_task1_retry_doc.py").splitlines()
    assert tracked_target == [str(TARGET)], "retry-doc test must not be renamed or duplicated"


def test_retry_doc_test_still_targets_current_architecture_retry_section():
    test_source = (ROOT / TARGET).read_text(encoding="utf-8")
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "LLM 韌性中介層（retry 子系統）" in test_source
    section = re.search(
        r"^## LLM 韌性中介層（retry 子系統）\n(.*?)(?=^## |\Z)",
        architecture,
        re.S | re.M,
    )
    assert section, "ARCHITECTURE.md must still contain the retry subsystem section"

    section_body = section.group(1)
    for anchor in ("make_retry_config", "RetryConfig", "run_with_retries", "max_retries=0"):
        assert anchor in section_body
        assert anchor in test_source


def test_retry_doc_pytest_contract_passes_with_expected_count():
    python = ROOT / ".venv/bin/python"
    assert python.exists(), ".venv/bin/python is required by the acceptance command"

    result = subprocess.run(
        [str(python), "-m", "pytest", str(TARGET), "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "10 passed, 1 skipped" in output
