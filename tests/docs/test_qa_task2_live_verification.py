from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPO = "x812033727/Ti"
TAG = "v0.2.0"
RUN_ID = "27905531397"
LIVE_ENV = "RUN_LIVE_TASK2_QA"

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_ENV) != "1",
    reason=f"live GitHub verification; set {LIVE_ENV}=1 to run",
)


def _run(
    command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _tmp_file(stem: str) -> Path:
    tmpdir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    return tmpdir / f"task2-qa-{stem}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.raw.log"


def _write_raw_log(
    path: Path, command: list[str], result: subprocess.CompletedProcess[str]
) -> None:
    path.write_text(
        "\n".join(
            [
                f"COMMAND={' '.join(command)}",
                "STDOUT:",
                result.stdout.rstrip(),
                "STDERR:",
                result.stderr.rstrip(),
                f"EXIT_CODE={result.returncode}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _load_checker_module():
    checker_path = ROOT / "scripts" / "check_release_body_structure.py"
    spec = importlib.util.spec_from_file_location("qa_task2_checker", checker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task2_live_prereqs_checker_output_and_verdict_match():
    auth = _run(["gh", "auth", "status"])
    assert auth.returncode == 0, auth.stdout + auth.stderr

    release = _run(
        [
            "gh",
            "release",
            "view",
            TAG,
            "--repo",
            REPO,
            "--json",
            "tagName,isDraft,isPrerelease,url,body",
        ]
    )
    assert release.returncode == 0, release.stdout + release.stderr
    release_json = json.loads(release.stdout)
    assert release_json["tagName"] == TAG
    assert release_json["isDraft"] is False
    assert release_json["isPrerelease"] is False
    assert release_json["url"] == f"https://github.com/{REPO}/releases/tag/{TAG}"
    assert len(release_json["body"]) > 0

    run = _run(
        [
            "gh",
            "run",
            "view",
            RUN_ID,
            "--repo",
            REPO,
            "--json",
            "databaseId,event,status,conclusion,workflowName,url",
        ]
    )
    assert run.returncode == 0, run.stdout + run.stderr
    run_json = json.loads(run.stdout)
    assert run_json == {
        "conclusion": "success",
        "databaseId": int(RUN_ID),
        "event": "release",
        "status": "completed",
        "url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}",
        "workflowName": "Release smoke",
    }

    checker_env = os.environ.copy()
    checker_env["PYTHONPATH"] = "."
    checker_command = ["python3", "scripts/check_release_body_structure.py"]
    checker = _run(checker_command, env=checker_env)
    checker_raw = _tmp_file("checker")
    _write_raw_log(checker_raw, ["env", "PYTHONPATH=.", *checker_command], checker)
    print(f"TASK2_CHECKER_RAW={checker_raw}")
    assert checker.returncode == 0, checker_raw.read_text(encoding="utf-8")
    assert "核對通過" in checker.stdout

    verdict = json.loads(
        (ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json").read_text(
            encoding="utf-8"
        )
    )
    evidence = json.loads(
        (ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json").read_text(encoding="utf-8")
    )
    checker_module = _load_checker_module()
    version = checker_module.pyproject_version()
    body = checker_module.normalize(evidence["gh_release_view"]["body"])
    rest_body = checker_module.normalize(evidence["rest_release_by_tag_subset"]["body"])

    recomputed_checks = {
        "雙來源正規化後逐字相等(gh vs REST)": body == rest_body,
        "頂部第一個頂層## 區塊": checker_module.first_top_level_h2(body),
        "頂部即 Breaking 置頂": checker_module.first_top_level_h2(body)
        == checker_module.BREAKING_HEADING,
        "四要素齊(①行為變動②原因③before/after④生效版本)": all(
            anchor in body and any(keyword.lower() in body.lower() for keyword in semantics)
            for _name, anchor, semantics in checker_module.FOUR_ELEMENTS
        ),
        "生效版本逐字對應_自0.2.0起": f"自 `{version}` 起" in body or f"自 {version} 起" in body,
        "逃生艙_TI_REQUIRE_CHOWN=warn/off": "TI_REQUIRE_CHOWN=warn" in body
        and "TI_REQUIRE_CHOWN=off" in body,
    }
    problems = checker_module.check(evidence, version)

    assert checker.stdout.count("EXIT_CODE=") == 0
    assert verdict["verdict"] == "PASS"
    assert verdict["problems"] == []
    assert problems == []
    assert recomputed_checks == verdict["checks"]

    body_sha256_exact = hashlib.sha256(
        evidence["gh_release_view"]["body"].encode("utf-8")
    ).hexdigest()
    body_sha256_with_newline = hashlib.sha256(
        (evidence["gh_release_view"]["body"] + "\n").encode("utf-8")
    ).hexdigest()
    body_sha256_summary = {
        "verdict_has_body_sha256": "body_sha256" in verdict,
        "evidence_body_sha256": evidence["body_sha256"],
        "verdict_body_sha256": verdict.get("body_sha256"),
        "exact_body_sha256": body_sha256_exact,
        "with_printed_newline_sha256": body_sha256_with_newline,
        "matches_exact": evidence["body_sha256"] == body_sha256_exact,
        "matches_with_printed_newline": evidence["body_sha256"] == body_sha256_with_newline,
        "verdict_matches_with_newline": verdict.get("body_sha256") == body_sha256_with_newline,
        "verdict_matches_exact": verdict.get("body_sha256") == body_sha256_exact,
        "verdict_with_printed_newline_field_matches": verdict.get("body_sha256_with_newline")
        == body_sha256_with_newline,
        "verdict_exact_field_matches": verdict.get("body_sha256_exact") == body_sha256_exact,
    }
    summary_path = _tmp_file("body-sha256-summary")
    summary_path.write_text(
        json.dumps(body_sha256_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"TASK2_BODY_SHA256_SUMMARY={summary_path}")
    assert verdict["body_sha256"] == evidence["body_sha256"]
    assert verdict.get("body_sha256") in {body_sha256_exact, body_sha256_with_newline}
    assert verdict.get("body_sha256_with_newline") == body_sha256_with_newline
    assert verdict.get("body_sha256_exact") == body_sha256_exact
    assert evidence["body_sha256"] in {body_sha256_exact, body_sha256_with_newline}
