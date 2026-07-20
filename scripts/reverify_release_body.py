"""重跑 v0.2.0 release body 線上證據並保存原始輸出。

本腳本是手動重驗工具，不進 CI；它會打 GitHub CLI / REST、執行既有結構檢查，
並把 raw stdout/stderr 與 evidence 逐項比對結果寫成 JSON。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_release_body_structure as body_check  # noqa: E402
from studio.release_note import BREAKING_HEADING  # noqa: E402

DEFAULT_REPO = "x812033727/Ti"
DEFAULT_TAG = "v0.2.0"
ONLINE_EVIDENCE = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"
STRUCTURE_EVIDENCE = ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json"
BODY_SHA256_RULE = {
    "algorithm": "sha256",
    "source": "gh_release_view.body",
    "bytes": "UTF-8 encoding of the parsed JSON string exactly",
    "newline": "no added newline",
    "normalization": "none",
    "format": "lowercase 64-character hexadecimal",
}


def command_text(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def run_capture(argv: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        argv,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "command": command_text(argv),
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_stdout_json(result: dict[str, Any], label: str) -> dict[str, Any]:
    if result["exit_code"] != 0:
        raise RuntimeError(f"{label} command failed: {result['stderr']}")
    return json.loads(result["stdout"])


def body_sha256_for_release_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_checks(evidence: dict[str, Any], version: str) -> dict[str, Any]:
    gh = body_check.normalize(evidence["gh_release_view"]["body"])
    rest = body_check.normalize(evidence["rest_release_by_tag_subset"]["body"])
    first_h2 = body_check.first_top_level_h2(gh)
    lower_body = gh.lower()
    return {
        "雙來源正規化後逐字相等(gh vs REST)": gh == rest,
        "頂部第一個頂層## 區塊": first_h2,
        "頂部即 Breaking 置頂": first_h2 == BREAKING_HEADING,
        "四要素齊(①行為變動②原因③before/after④生效版本)": all(
            anchor in gh and any(keyword.lower() in lower_body for keyword in semantics)
            for _, anchor, semantics in body_check.FOUR_ELEMENTS
        ),
        "生效版本逐字對應_自0.2.0起": (f"自 `{version}` 起" in gh or f"自 {version} 起" in gh),
        "逃生艙_TI_REQUIRE_CHOWN=warn/off": (
            "TI_REQUIRE_CHOWN=warn" in gh and "TI_REQUIRE_CHOWN=off" in gh
        ),
    }


def build_payload(
    *,
    captured_at_utc: str,
    repo: str,
    tag: str,
    gh_result: dict[str, Any],
    rest_result: dict[str, Any],
    checker_result: dict[str, Any],
    online_evidence: dict[str, Any],
    structure_evidence: dict[str, Any],
) -> dict[str, Any]:
    gh_payload = parse_stdout_json(gh_result, "gh release view")
    rest_payload = parse_stdout_json(rest_result, "REST release by tag")

    actual_evidence = {
        "captured_at_utc": captured_at_utc,
        "repo": repo,
        "tag": tag,
        "gh_release_view_command": gh_result["command"],
        "rest_get_command": rest_result["command"],
        "rest_endpoint": f"GET /repos/{repo}/releases/tags/{tag}",
        "body_match": body_check.normalize(gh_payload["body"])
        == body_check.normalize(rest_payload["body"]),
        "tag_match": gh_payload["tagName"] == rest_payload["tag_name"] == tag,
        "url_match": gh_payload["url"] == rest_payload["html_url"],
        "body_sha256": body_sha256_for_release_body(gh_payload["body"]),
        "body_sha256_rule": BODY_SHA256_RULE,
        "gh_release_view": gh_payload,
        "rest_release_by_tag_subset": rest_payload,
    }

    version = body_check.pyproject_version()
    problems = body_check.check(actual_evidence, version)
    checks = build_checks(actual_evidence, version)
    actual_structure = {
        "verdict": "PASS" if not problems else "FAIL",
        "checks": checks,
        "problems": problems,
    }

    comparisons = {
        "body_sha256": {
            "actual": actual_evidence["body_sha256"],
            "expected_from_evidence": online_evidence["body_sha256"],
            "matches_evidence": actual_evidence["body_sha256"] == online_evidence["body_sha256"],
        },
        "body_sha256_rule": {
            "actual": actual_evidence["body_sha256_rule"],
            "expected_from_evidence": online_evidence.get("body_sha256_rule"),
            "matches_evidence": actual_evidence["body_sha256_rule"]
            == online_evidence.get("body_sha256_rule"),
        },
        "body_match": {
            "actual": actual_evidence["body_match"],
            "expected_from_evidence": online_evidence["body_match"],
            "matches_evidence": actual_evidence["body_match"] == online_evidence["body_match"],
        },
        "tag_match": {
            "actual": actual_evidence["tag_match"],
            "expected_from_evidence": online_evidence["tag_match"],
            "matches_evidence": actual_evidence["tag_match"] == online_evidence["tag_match"],
        },
        "url_match": {
            "actual": actual_evidence["url_match"],
            "expected_from_evidence": online_evidence["url_match"],
            "matches_evidence": actual_evidence["url_match"] == online_evidence["url_match"],
        },
        "structure": {
            "actual_verdict": actual_structure["verdict"],
            "expected_verdict": structure_evidence["verdict"],
            "verdict_matches_evidence": actual_structure["verdict"]
            == structure_evidence["verdict"],
            "checks_match_evidence": actual_structure["checks"] == structure_evidence["checks"],
            "problems_match_evidence": actual_structure["problems"]
            == structure_evidence["problems"],
            "checker_command_exit_zero": checker_result["exit_code"] == 0,
        },
    }

    all_match = all(
        item["matches_evidence"] for key, item in comparisons.items() if key != "structure"
    )
    structure_match = all(comparisons["structure"].values())
    command_ok = (
        gh_result["exit_code"] == 0
        and rest_result["exit_code"] == 0
        and checker_result["exit_code"] == 0
    )

    return {
        "task": "重跑證據 #1/#2 線上重驗",
        "captured_at_utc": captured_at_utc,
        "repo": repo,
        "tag": tag,
        "source_evidence": {
            "online_body": str(ONLINE_EVIDENCE.relative_to(ROOT)),
            "body_structure_verdict": str(STRUCTURE_EVIDENCE.relative_to(ROOT)),
        },
        "raw_commands": {
            "gh_release_view": gh_result,
            "rest_release_by_tag": rest_result,
            "body_structure_checker": checker_result,
        },
        "actual_evidence_summary": {
            "body_sha256": actual_evidence["body_sha256"],
            "body_sha256_rule": actual_evidence["body_sha256_rule"],
            "body_match": actual_evidence["body_match"],
            "tag_match": actual_evidence["tag_match"],
            "url_match": actual_evidence["url_match"],
        },
        "actual_structure": actual_structure,
        "comparisons": comparisons,
        "verdict": "PASS" if command_ok and all_match and structure_match else "FAIL",
    }


def default_output_path(tag: str) -> Path:
    today = datetime.now(UTC).date().isoformat()
    safe_tag = tag.replace("/", "-")
    return ROOT / "docs" / "evidence" / f"release-{safe_tag}-online-reverify-{today}.json"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output = args.output or default_output_path(args.tag)
    captured_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    gh_cmd = ["timeout", "60", "gh", "release", "view", args.tag, "--json", "body,tagName,url"]
    rest_cmd = [
        "timeout",
        "60",
        "gh",
        "api",
        f"repos/{args.repo}/releases/tags/{args.tag}",
        "--jq",
        "{body,tag_name,html_url,id,created_at,published_at}",
    ]
    checker_cmd = [
        "timeout",
        "60",
        "python3",
        "scripts/check_release_body_structure.py",
    ]

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # subprocess.run inherits env by default; setting PYTHONIOENCODING globally keeps CLI text stable.
    os.environ.update({"PYTHONIOENCODING": env["PYTHONIOENCODING"]})

    payload = build_payload(
        captured_at_utc=captured_at_utc,
        repo=args.repo,
        tag=args.tag,
        gh_result=run_capture(gh_cmd),
        rest_result=run_capture(rest_cmd),
        checker_result=run_capture(checker_cmd),
        online_evidence=load_json(ONLINE_EVIDENCE),
        structure_evidence=load_json(STRUCTURE_EVIDENCE),
    )

    output = output if output.is_absolute() else ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        output_label = str(output.relative_to(ROOT))
    except ValueError:
        output_label = str(output)
    print(json.dumps({"output": output_label, "verdict": payload["verdict"]}))
    return 0 if payload["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
