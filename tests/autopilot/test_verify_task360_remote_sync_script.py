from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_task360_remote_sync.sh"

LOCAL_SHA = "8d1a8882c9dde6073f1c2e0282eaa81915b9ad53"
REMOTE_OK_B64 = "eyJ2ZXJkaWN0Ijoib2sifQo="
REMOTE_WITH_BODY_SHA_B64 = "eyJib2R5X3NoYTI1Nl9leGFjdCI6InNob3VsZC1iZS1yZWplY3RlZCJ9Cg=="


def _exported_bash_func(body: str) -> str:
    return f"() {{\n{body}\n}}"


def _git_stub(local_sha: str = LOCAL_SHA) -> str:
    return f"""
if [ "${{1:-}}" = "rev-parse" ] && [ "${{2:-}}" = "autopilot/task-360" ]; then
  printf '%s\\n' "{local_sha}"
else
  printf 'unexpected git args: %s %s\\n' "${{1:-}}" "${{2:-}}" >&2
  return 42
fi
"""


def _gh_stub(
    *,
    remote_sha: str = LOCAL_SHA,
    compare_statuses: dict[str, str] | None = None,
    remote_content_b64: str = REMOTE_OK_B64,
) -> str:
    statuses = {
        "baa6a9c": "ahead",
        "4d50a11": "ahead",
        "b7f3932": "ahead",
    }
    statuses.update(compare_statuses or {})
    return f"""
if [ "${{1:-}}" != "api" ]; then
  printf 'unexpected gh command: %s\\n' "${{1:-}}" >&2
  return 43
fi
case "${{2:-}}" in
  repos/x812033727/Ti/branches/autopilot%2Ftask-360)
    printf '%s\\n' "{remote_sha}"
    ;;
  repos/x812033727/Ti/compare/baa6a9c...autopilot%2Ftask-360)
    printf '%s\\n' "{statuses["baa6a9c"]}"
    ;;
  repos/x812033727/Ti/compare/4d50a11...autopilot%2Ftask-360)
    printf '%s\\n' "{statuses["4d50a11"]}"
    ;;
  repos/x812033727/Ti/compare/b7f3932...autopilot%2Ftask-360)
    printf '%s\\n' "{statuses["b7f3932"]}"
    ;;
  repos/x812033727/Ti/contents/docs/evidence/release-v0.2.0-body-structure-verdict.json\\?ref=autopilot%2Ftask-360)
    printf '%s\\n' "{remote_content_b64}"
    ;;
  *)
    printf 'unexpected gh route: %s\\n' "${{2:-}}" >&2
    return 44
    ;;
esac
"""


def _run_with_stubs(
    *,
    gh_body: str | None = None,
    git_body: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["BASH_FUNC_git%%"] = _exported_bash_func(git_body or _git_stub())
    env["BASH_FUNC_gh%%"] = _exported_bash_func(gh_body or _gh_stub())
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_script_contract_is_task_specific_and_fetch_free():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "task-360 一次性同步驗證，僅本場使用，勿當通用入口" in source
    assert "set -euo pipefail" in source
    assert 'REPO="x812033727/Ti"' in source
    assert 'BRANCH="autopilot/task-360"' in source
    assert 'BRANCH_ENCODED="autopilot%2Ftask-360"' in source
    assert "release-v0.2.0-body-structure-verdict.json" in source
    assert "COMPARE_BASES=(baa6a9c 4d50a11 b7f3932)" in source
    assert "--jq .commit.sha" in source
    assert "--jq .status" in source
    assert re.search(r"git\s+(fetch|pull)\b", source) is None
    assert re.search(r"if\s+grep\s+-q\s+'body_sha256'", source)


def test_script_syntax_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_stubbed_happy_path_reports_all_required_passes():
    result = _run_with_stubs()

    assert result.returncode == 0, result.stderr
    assert f"local_sha={LOCAL_SHA}" in result.stdout
    assert f"remote_sha={LOCAL_SHA}" in result.stdout
    assert "[a] 本地/遠端 sha 相等: PASS" in result.stdout
    assert "[b] compare baa6a9c...autopilot/task-360 status=ahead: PASS" in result.stdout
    assert "[b] compare 4d50a11...autopilot/task-360 status=ahead: PASS" in result.stdout
    assert "[b] compare b7f3932...autopilot/task-360 status=ahead: PASS" in result.stdout
    assert "[c] 遠端檔案不含 body_sha256: PASS" in result.stdout
    assert "RESULT: PASS" in result.stdout


def test_stubbed_remote_sha_mismatch_fails_before_result_pass():
    result = _run_with_stubs(
        gh_body=_gh_stub(remote_sha="1111111111111111111111111111111111111111")
    )

    assert result.returncode != 0
    assert "[a] 本地與遠端 sha 不一致" in result.stderr
    assert "RESULT: PASS" not in result.stdout


def test_stubbed_non_ancestor_compare_status_fails():
    result = _run_with_stubs(gh_body=_gh_stub(compare_statuses={"4d50a11": "behind"}))

    assert result.returncode != 0
    assert "[b] compare 4d50a11...autopilot/task-360 status=behind" in result.stderr
    assert "RESULT: PASS" not in result.stdout


def test_stubbed_remote_verdict_rejects_any_body_sha256_field_name():
    result = _run_with_stubs(gh_body=_gh_stub(remote_content_b64=REMOTE_WITH_BODY_SHA_B64))

    assert result.returncode != 0
    assert "[c] 遠端檔案仍含 body_sha256" in result.stderr
    assert "RESULT: PASS" not in result.stdout
