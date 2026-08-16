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


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    senior_text = os.environ.get("SENIOR_REVIEW_TEXT", "")
    workflow_log_text = os.environ.get("WORKFLOW_LOG_TEXT", "")

    local_head = run(["git", "rev-parse", "HEAD"])
    pr_view_1 = json.loads(run(["gh", "pr", "view", PR_NUMBER, "--json", "headRefOid,url,body"]))
    pr_view_2 = json.loads(run(["gh", "pr", "view", PR_NUMBER, "--json", "headRefOid"]))
    files = run(["gh", "pr", "diff", PR_NUMBER, "--name-only"]).splitlines()
    patch = run(["gh", "pr", "diff", PR_NUMBER, "--patch"])
    stat = run(["git", "show", "--stat", "HEAD"])

    require(local_head == EXPECTED_SHA, f"local HEAD mismatch: {local_head}")
    require(pr_view_1["headRefOid"] == EXPECTED_SHA, f"PR head mismatch: {pr_view_1['headRefOid']}")
    require(pr_view_2["headRefOid"] == pr_view_1["headRefOid"], "PR head drifted between reads")
    require(files == EXPECTED_FILES, f"unexpected diff files: {files}")

    body = pr_view_1["body"]
    require("## 動機" in body, "PR body missing 動機 section")
    require("## 如何驗證" in body, "PR body missing 如何驗證 section")

    for token in [
        "len(t) == 5",
        't[2] == ":"',
        "t[:2].isdigit()",
        "t[3:].isdigit()",
        'time": "8:30"',
        'time": "08:3"',
    ]:
        require(token in patch, f"patch missing expected token: {token}")

    require("studio/schedules.py" in stat, "git show --stat missing studio/schedules.py")
    require("tests/autopilot/test_schedules.py" in stat, "git show --stat missing test file")
    require("2 files changed" in stat, "HEAD stat should remain limited to two files")

    require("決議: 核可" in senior_text, "senior marker missing exact text")
    require(EXPECTED_SHA in senior_text, "senior marker missing expected head sha")

    for token in FORBIDDEN_WORKFLOW_TOKENS:
        require(token not in workflow_log_text, f"forbidden workflow command found: {token}")

    print(f"PASS pr={PR_NUMBER} head={EXPECTED_SHA} files={','.join(files)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1)
