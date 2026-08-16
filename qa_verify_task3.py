import json
import os
import subprocess
import sys


PR_NUMBER = "617"
EXPECTED_SHA = "8c03930fa7c638bd19eca67595b2529798153552"
EXPECTED_FILES = [
    "studio/schedules.py",
    "tests/autopilot/test_schedules.py",
]
FORBIDDEN_WORKFLOW_TOKENS = [
    ".venv/bin/python -m pytest",
    " python -m pytest",
    "python3 -m pytest",
    "pip install",
    "nohup ",
]


def run(args: list[str]) -> str:
    proc = subprocess.run(args, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(args)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc.stdout.strip()


def record(failures: list[str], condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    failures: list[str] = []
    qa_text = os.environ.get("QA_REVIEW_TEXT", "")
    senior_text = os.environ.get("SENIOR_REVIEW_TEXT", "")
    workflow_log_text = os.environ.get("WORKFLOW_LOG_TEXT", "")

    pr_view_1 = json.loads(run(["gh", "pr", "view", PR_NUMBER, "--json", "headRefOid,url,body"]))
    pr_view_2 = json.loads(run(["gh", "pr", "view", PR_NUMBER, "--json", "headRefOid"]))
    files = run(["gh", "pr", "diff", PR_NUMBER, "--name-only"]).splitlines()
    patch = run(["gh", "pr", "diff", PR_NUMBER, "--patch"])
    pr_head = pr_view_1["headRefOid"]
    stat = run(["git", "show", "--stat", pr_head])

    record(failures, pr_head == EXPECTED_SHA, f"PR head mismatch: {pr_head}")
    record(failures, pr_view_2["headRefOid"] == pr_head, "PR head drifted between reads")
    record(failures, files == EXPECTED_FILES, f"unexpected diff files: {files}")

    body = pr_view_1["body"]
    record(failures, "## 動機" in body, "PR body missing 動機 section")
    record(failures, "## 如何驗證" in body, "PR body missing 如何驗證 section")

    for token in [
        "len(t) == 5",
        't[2] == ":"',
        "t[:2].isdigit()",
        "t[3:].isdigit()",
        'time": "8:30"',
        'time": "08:3"',
    ]:
        record(failures, token in patch, f"patch missing expected token: {token}")

    record(failures, "studio/schedules.py" in stat, "git show --stat missing studio/schedules.py")
    record(failures, "tests/autopilot/test_schedules.py" in stat, "git show --stat missing test file")
    record(failures, "2 files changed" in stat, "PR head stat should remain limited to two files")

    record(failures, "驗證: PASS" in qa_text, "qa marker missing exact text")
    record(failures, EXPECTED_SHA in qa_text, "qa marker missing expected head sha")
    record(failures, "決議: 核可" in senior_text, "senior marker missing exact text")
    record(failures, EXPECTED_SHA in senior_text, "senior marker missing expected head sha")

    for token in FORBIDDEN_WORKFLOW_TOKENS:
        record(failures, token not in workflow_log_text, f"forbidden workflow command found: {token}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        print(f"checked pr={PR_NUMBER} head={pr_head} files={','.join(files)}")
        return 1

    print(f"PASS pr={PR_NUMBER} head={pr_head} files={','.join(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
