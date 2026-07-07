from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts import reverify_release_body

ROOT = Path(__file__).resolve().parents[2]
ONLINE = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"
STRUCTURE = ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json"


def test_body_structure_checker_can_run_without_pythonpath():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        ["python3", "scripts/check_release_body_structure.py"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "核對通過" in result.stdout


def test_reverify_payload_preserves_raw_outputs_and_matches_evidence():
    online = json.loads(ONLINE.read_text(encoding="utf-8"))
    structure = json.loads(STRUCTURE.read_text(encoding="utf-8"))

    gh_stdout = json.dumps(online["gh_release_view"], ensure_ascii=False)
    rest_stdout = json.dumps(online["rest_release_by_tag_subset"], ensure_ascii=False)
    checker_stdout = (
        "== v0.2.0 線上 body 結構斷言核對 ==\n"
        "核對通過（雙來源一致＋頂部 Breaking 置頂＋四要素齊＋逃生艙齊＋生效版本逐字對應）。\n"
    )

    payload = reverify_release_body.build_payload(
        captured_at_utc="2026-07-07T00:00:00Z",
        repo="x812033727/Ti",
        tag="v0.2.0",
        gh_result={
            "command": "timeout 60 gh release view v0.2.0 --json body,tagName,url",
            "exit_code": 0,
            "stdout": gh_stdout,
            "stderr": "",
        },
        rest_result={
            "command": (
                "timeout 60 gh api repos/x812033727/Ti/releases/tags/v0.2.0 "
                "--jq '{body,tag_name,html_url,id,created_at,published_at}'"
            ),
            "exit_code": 0,
            "stdout": rest_stdout,
            "stderr": "",
        },
        checker_result={
            "command": (
                "timeout 60 env PYTHONPATH=. python3 scripts/check_release_body_structure.py"
            ),
            "exit_code": 0,
            "stdout": checker_stdout,
            "stderr": "",
        },
        online_evidence=online,
        structure_evidence=structure,
    )

    assert payload["verdict"] == "PASS"
    assert payload["raw_commands"]["gh_release_view"]["stdout"] == gh_stdout
    assert payload["raw_commands"]["rest_release_by_tag"]["stdout"] == rest_stdout
    assert payload["raw_commands"]["body_structure_checker"]["stdout"] == checker_stdout
    assert payload["comparisons"]["body_sha256"]["matches_evidence"] is True
    assert payload["comparisons"]["structure"]["checks_match_evidence"] is True
