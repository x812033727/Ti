"""QA online revalidation for release v0.2.0 evidence #1/#2.

This test intentionally calls GitHub through the same commands transcribed in
docs/release-e2e-closure-report.md. Raw command outputs are preserved under
/tmp/qa_task1_release_revalidation so a failed run can be reproduced without
rerunning the network calls immediately.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from _repo import REPO_ROOT

ROOT = REPO_ROOT
RAW_DIR = Path(os.environ.get("QA_TASK1_RAW_DIR", "/tmp/qa_task1_release_revalidation"))
ONLINE_EVIDENCE = ROOT / "docs/evidence/release-v0.2.0-online-body.json"
STRUCTURE_EVIDENCE = ROOT / "docs/evidence/release-v0.2.0-body-structure-verdict.json"
RERUN_EVIDENCE = ROOT / "docs/evidence/release-v0.2.0-rerun-20260706.json"
REPORT = ROOT / "docs/release-e2e-closure-report.md"


def _run_and_save(
    name: str, argv: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=65,
    )
    payload = {
        "argv": argv,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    (RAW_DIR / f"{name}.raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"{name} failed with rc={result.returncode}; raw output: {RAW_DIR / f'{name}.raw.json'}"
    )
    return result


def _normalize(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _first_top_level_h2(body: str) -> str | None:
    for raw in body.split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            return line
    return None


def test_task1_online_release_body_and_structure_match_existing_evidence():
    online_evidence = json.loads(ONLINE_EVIDENCE.read_text(encoding="utf-8"))
    structure_evidence = json.loads(STRUCTURE_EVIDENCE.read_text(encoding="utf-8"))
    rerun_evidence = json.loads(RERUN_EVIDENCE.read_text(encoding="utf-8"))

    gh_release = _run_and_save(
        "task1_gh_release_view",
        ["timeout", "60", "gh", "release", "view", "v0.2.0", "--json", "body,tagName,url"],
    )
    rest_release = _run_and_save(
        "task1_rest_release_by_tag",
        [
            "timeout",
            "60",
            "gh",
            "api",
            "repos/x812033727/Ti/releases/tags/v0.2.0",
            "--jq",
            "{body,tag_name,html_url,id,created_at,published_at}",
        ],
    )
    checker_env = os.environ.copy()
    checker_env["PYTHONPATH"] = "."
    checker_env.pop("TI_REQUIRE_CHOWN", None)
    checker = _run_and_save(
        "task1_check_release_body_structure",
        [
            "timeout",
            "60",
            "env",
            "PYTHONPATH=.",
            "python3",
            "scripts/check_release_body_structure.py",
        ],
        env=checker_env,
    )
    report = REPORT.read_text(encoding="utf-8")
    assert gh_release.stdout.strip() in report
    assert rest_release.stdout.strip() in report
    assert checker.stdout.strip() in report
    assert "docs/evidence/release-v0.2.0-rerun-20260706.json" in report
    assert rerun_evidence["all_matches_evidence"] is True
    for name, result in {
        "gh_release_view": gh_release,
        "rest_release_by_tag": rest_release,
        "body_structure_check": checker,
    }.items():
        stored = rerun_evidence["commands"][name]
        assert stored["exit_code"] == 0
        assert stored["stdout"] == result.stdout
        assert stored["stderr"] == result.stderr

    gh_data = json.loads(gh_release.stdout)
    rest_data = json.loads(rest_release.stdout)
    comparison = {
        "body_sha256": {
            "actual": hashlib.sha256((gh_data["body"] + "\n").encode("utf-8")).hexdigest(),
            "expected_from_evidence": online_evidence["body_sha256"],
        },
        "body_match": {
            "actual": gh_data["body"] == rest_data["body"],
            "expected_from_evidence": online_evidence["body_match"],
        },
        "tag_match": {
            "actual": gh_data["tagName"] == rest_data["tag_name"] == online_evidence["tag"],
            "expected_from_evidence": online_evidence["tag_match"],
        },
        "url_match": {
            "actual": gh_data["url"] == rest_data["html_url"],
            "expected_from_evidence": online_evidence["url_match"],
        },
        "structure_verdict": {
            "actual": "PASS" if checker.returncode == 0 else "FAIL",
            "expected_from_evidence": structure_evidence["verdict"],
        },
    }

    normalized_gh = _normalize(gh_data["body"])
    normalized_rest = _normalize(rest_data["body"])
    checks = {
        "雙來源正規化後逐字相等(gh vs REST)": normalized_gh == normalized_rest,
        "頂部第一個頂層## 區塊": _first_top_level_h2(normalized_gh),
        "頂部即 Breaking 置頂": _first_top_level_h2(normalized_gh)
        == structure_evidence["breaking_heading_constant"],
        "四要素齊(①行為變動②原因③before/after④生效版本)": all(
            token in normalized_gh
            for token in ("① 行為變動", "② 原因", "③ before / after", "④ 生效版本")
        ),
        "生效版本逐字對應_自0.2.0起": "自 `0.2.0` 起" in normalized_gh
        or "自 0.2.0 起" in normalized_gh,
        "逃生艙_TI_REQUIRE_CHOWN=warn/off": "TI_REQUIRE_CHOWN=warn" in normalized_gh
        and "TI_REQUIRE_CHOWN=off" in normalized_gh,
    }
    comparison["structure_checks"] = {
        key: {
            "actual": actual,
            "expected_from_evidence": structure_evidence["checks"][key],
        }
        for key, actual in checks.items()
    }
    (RAW_DIR / "task1_comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert (
        comparison["body_sha256"]["actual"] == comparison["body_sha256"]["expected_from_evidence"]
    )
    assert comparison["body_match"]["actual"] is comparison["body_match"]["expected_from_evidence"]
    assert comparison["tag_match"]["actual"] is comparison["tag_match"]["expected_from_evidence"]
    assert comparison["url_match"]["actual"] is comparison["url_match"]["expected_from_evidence"]
    assert (
        comparison["structure_verdict"]["actual"]
        == comparison["structure_verdict"]["expected_from_evidence"]
    )
    assert structure_evidence["problems"] == []
    assert "核對通過" in checker.stdout

    for key, result in comparison["structure_checks"].items():
        assert result["actual"] == result["expected_from_evidence"], (
            f"{key} mismatch; comparison: {RAW_DIR / 'task1_comparison.json'}"
        )
