from pathlib import Path
import hashlib
import re


REPO_ROOT = Path("/opt/ti-autopilot-work")
ARTIFACT_DIR = REPO_ROOT / ".qa_artifacts" / "task1_retry_doc"
REPORT = ARTIFACT_DIR / "verify_report.md"
SHA_FILE = ARTIFACT_DIR / "verify_report.md.sha256"

PYTEST_COMMAND = ".venv/bin/python -m pytest tests/test_task1_retry_doc.py -q"


def read_report() -> str:
    assert REPORT == Path("/opt/ti-autopilot-work/.qa_artifacts/task1_retry_doc/verify_report.md")
    assert REPORT.is_file(), f"missing authority report: {REPORT}"
    return REPORT.read_text(encoding="utf-8")


def embedded_sha(text: str) -> str:
    matches = re.findall(r"(?m)^([0-9a-f]{64})  verify_report\.md$", text)
    assert len(matches) == 1, "report must contain exactly one embedded sha256 line"
    return matches[0]


def test_report_declares_fixed_absolute_authority_path():
    text = read_report()

    assert str(REPORT) in text, "report must declare its fixed absolute authority path"
    assert "唯一權威" in text, "report must contain a unique-authority statement"


def test_all_residual_artifacts_are_named_in_ignore_list():
    text = read_report()
    allowed = {"verify_report.md", "verify_report.md.sha256"}
    residuals = sorted(path.name for path in ARTIFACT_DIR.iterdir() if path.name not in allowed)

    missing = [name for name in residuals if name not in text]
    assert not missing, (
        "residual artifacts exist but are not explicitly listed as ignored/non-authority: "
        + ", ".join(missing)
    )


def test_pytest_pass_skip_evidence_and_semantics_are_recorded():
    text = read_report()

    assert PYTEST_COMMAND in text
    assert "PASSED tests/test_task1_retry_doc.py::test_no_py_changed" in text
    assert re.search(r"\b11 passed in [0-9.]+s\b", text)

    assert "SKIPPED [1] tests/test_task1_retry_doc.py:188" in text
    assert re.search(r"\b10 passed, 1 skipped in [0-9.]+s\b", text)

    assert "skip ≠ pass" in text
    assert "設計性" in text
    assert "非 pass" in text


def test_merge_premise_commands_and_outputs_are_recorded():
    text = read_report()
    required_fragments = [
        "git fetch origin main",
        "git show origin/main:tests/test_task1_retry_doc.py",
        "merge-base HEAD origin/main",
        "git rev-parse HEAD",
        "git rev-parse origin/main",
        "MERGED",
        "3abb092244aec5201b73ed97a7a5c858fe103e00",
    ]

    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, "merge-premise command/output evidence missing: " + ", ".join(missing)


def test_placeholder_based_self_sha256_matches_external_file():
    text = read_report()
    sha = embedded_sha(text)

    placeholder_text = re.sub(
        r"(?m)^[0-9a-f]{64}  verify_report\.md$",
        "__SHA256_PLACEHOLDER__  verify_report.md",
        text,
        count=1,
    )
    calculated = hashlib.sha256(placeholder_text.encode("utf-8")).hexdigest()

    assert calculated == sha
    assert text.count(sha) == 1, "embedded sha must not appear in prose"
    assert SHA_FILE.is_file(), f"missing external sha file: {SHA_FILE}"
    assert SHA_FILE.read_text(encoding="utf-8").strip() == f"{sha}  verify_report.md"
