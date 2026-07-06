from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"
REPORT = ROOT / "docs" / "release-e2e-closure-report.md"
REVERIFY_SCRIPT = ROOT / "scripts" / "reverify-release-v020-task1.sh"


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _json_from_stdout(stdout: str) -> dict:
    start = stdout.find("{")
    assert start != -1, f"stdout did not contain a JSON object:\n{stdout}"
    return json.loads(stdout[start:])


def _task1_command_section(report_text: str) -> str:
    marker = "### 2026-07-06"
    start = report_text.index(marker)
    end = report_text.index("#2", start)
    return report_text[start:end]


def test_task1_reverify_script_fetches_fresh_sources_and_records_gap():
    before = EVIDENCE.read_bytes()
    tmpdir = ROOT / ".qa-tmp" / "task1-release-identity"
    tmpdir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMPDIR": str(tmpdir)}

    result = _run(["timeout", "60", "bash", str(REVERIFY_SCRIPT)], env=env)

    after = EVIDENCE.read_bytes()
    assert after == before, "task #1 revalidation must not mutate evidence"
    assert result.returncode == 0, (
        "reverify script failed; stdout/stderr:\n"
        f"{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    payload = _json_from_stdout(result.stdout)
    assert payload["status"] == "COMPARED"
    assert payload["comparisons"]["tagName"]["match"] is True
    assert payload["comparisons"]["url"]["match"] is True

    body_cmp = payload["comparisons"]["body_sha256"]
    assert body_cmp["match"] is False
    assert body_cmp["gh"] == body_cmp["rest"]
    assert body_cmp["evidence"] != body_cmp["gh"]

    preflight_log = Path(payload["generated_outputs"]["preflight_log"])
    assert preflight_log.is_file()
    log_text = preflight_log.read_text(encoding="utf-8")
    for label in ("gh-auth-status", "network", "release-readable"):
        assert re.search(rf"\[{re.escape(label)}\] exit=0\b", log_text), log_text

    for key in ("gh_release_view", "rest_release_by_tag"):
        raw_path = Path(payload["raw_outputs"][key])
        assert raw_path.is_file(), f"missing raw output: {raw_path}"
        assert raw_path.parent == tmpdir
        assert "task1" in raw_path.name
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        assert raw.get("body"), f"{key} body must be non-empty"

    volatile = payload["volatile_recorded_not_compared"]["rest_release_by_tag"]
    assert volatile["id"]
    assert volatile["created_at"]
    assert volatile["published_at"]


def test_task1_reverify_script_uses_jq_e_and_diff_for_identity_fields():
    script = REVERIFY_SCRIPT.read_text(encoding="utf-8")
    missing = [
        token
        for token in ("jq -e", "diff -u")
        if token not in script
    ]
    assert not missing, (
        "task #1 acceptance requires commandized jq+diff comparison; "
        f"missing from {REVERIFY_SCRIPT}: {missing}"
    )


def test_report_task1_copy_paste_commands_include_raw_tmpdir_and_jq_diff():
    section = _task1_command_section(REPORT.read_text(encoding="utf-8"))
    required_snippets = [
        "TMP=\"${TMPDIR:-/tmp}\"",
        "gh release view v0.2.0 --json body,tagName,url",
        "gh api repos/x812033727/Ti/releases/tags/v0.2.0",
        "task1",
        "jq -e",
        "diff -u",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in section]
    assert not missing, (
        "task #1 report must contain copy-pasteable raw-output and jq+diff "
        f"commands; missing snippets: {missing}"
    )
