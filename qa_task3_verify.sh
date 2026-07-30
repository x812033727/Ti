#!/usr/bin/env bash
set -u

BASE_REF="${BASE_REF:-origin/main}"
HEAD_REF="${HEAD_REF:-origin/fix/nightly-studio-inspection}"
PR_NUMBER="${PR_NUMBER:-560}"

failures=0

pass() {
  printf 'PASS: %s\n' "$1"
}

fail() {
  failures=$((failures + 1))
  printf 'FAIL: %s\n' "$1"
}

contains() {
  local haystack="$1"
  local needle="$2"
  [[ "$haystack" == *"$needle"* ]]
}

if git rev-parse --verify "$BASE_REF^{commit}" >/dev/null 2>&1; then
  pass "base ref exists: $BASE_REF"
else
  fail "base ref missing: $BASE_REF"
fi

if git rev-parse --verify "$HEAD_REF^{commit}" >/dev/null 2>&1; then
  pass "head ref exists: $HEAD_REF"
else
  fail "head ref missing: $HEAD_REF"
fi

pr_meta="$(gh pr view "$PR_NUMBER" --json state,isDraft,baseRefName,headRefName --jq '[.state, (.isDraft|tostring), .baseRefName, .headRefName] | @tsv' 2>/dev/null)"
if [[ "$pr_meta" == $'OPEN\tfalse\tmain\tfix/nightly-studio-inspection' ]]; then
  pass "PR #$PR_NUMBER is open, non-draft, main <- fix/nightly-studio-inspection"
else
  fail "PR metadata mismatch: ${pr_meta:-<empty>}"
fi

diff_files="$(git diff --name-only "$BASE_REF...$HEAD_REF" | sort)"
expected_files=$'studio/config.py\ntests/settings/test_env_numeric_hardening.py'
if [[ "$diff_files" == "$expected_files" ]]; then
  pass "diff scope is exactly config plus matching settings test"
else
  fail "unexpected diff files: ${diff_files//$'\n'/, }"
fi

if git diff --check "$BASE_REF...$HEAD_REF" >/dev/null; then
  pass "git diff --check has no whitespace errors"
else
  fail "git diff --check reported whitespace errors"
fi

config_src="$(git show "$HEAD_REF:studio/config.py")"
for line in \
  'RLIMIT_MEM_MB = _env_int("TI_RLIMIT_MEM_MB", 4096)' \
  'RLIMIT_CPU_S = _env_int("TI_RLIMIT_CPU_S", 300)' \
  'RLIMIT_FSIZE_MB = _env_int("TI_RLIMIT_FSIZE_MB", 512)'
do
  count="$(grep -F -c "$line" <<<"$config_src" || true)"
  if [[ "$count" == "2" ]]; then
    pass "config top-level and reload both contain: $line"
  else
    fail "expected two config occurrences, got $count: $line"
  fi
done

if contains "$config_src" 'global RLIMIT_MEM_MB, RLIMIT_CPU_S, RLIMIT_FSIZE_MB'; then
  pass "reload declares all three RLIMIT globals"
else
  fail "reload missing RLIMIT globals declaration"
fi

test_src="$(git show "$HEAD_REF:tests/settings/test_env_numeric_hardening.py")"
for needle in \
  'def test_config_reload_updates_rlimit_values(monkeypatch):' \
  'monkeypatch.setenv("TI_RLIMIT_MEM_MB", "256")' \
  'monkeypatch.setenv("TI_RLIMIT_CPU_S", "45")' \
  'monkeypatch.setenv("TI_RLIMIT_FSIZE_MB", "8")' \
  'assert config.RLIMIT_MEM_MB == 256' \
  'assert config.RLIMIT_CPU_S == 45' \
  'assert config.RLIMIT_FSIZE_MB == 8' \
  'monkeypatch.delenv("TI_RLIMIT_MEM_MB", raising=False)' \
  'config.reload()'
do
  if contains "$test_src" "$needle"; then
    pass "test contains: $needle"
  else
    fail "test missing: $needle"
  fi
done

pr_body="$(gh pr view "$PR_NUMBER" --json body --jq .body 2>/dev/null)"
if contains "$pr_body" 'git diff --check'; then
  pass "PR body mentions static git diff validation"
else
  fail "PR body does not mention git diff --check"
fi

if grep -Eiq '(^|[^[:alnum:]_])pytest([^[:alnum:]_]|$)|pip[[:space:]]+install|啟動服務|nohup|uvicorn|flask run|npm run dev' <<<"$pr_body"; then
  fail "PR body claims prohibited runtime validation was performed"
else
  pass "PR body does not claim pytest, pip install, or service validation"
fi

if grep -Eiq 'ro-bind|tests/sandbox|sandbox/test_qa_ro_bind' <<<"$pr_body"; then
  fail "PR body contains out-of-scope ro-bind/sandbox text not present in this diff"
else
  pass "PR body scope matches current diff topic"
fi

if (( failures == 0 )); then
  printf 'RESULT: PASS\n'
else
  printf 'RESULT: FAIL (%d failure(s))\n' "$failures"
fi

exit "$failures"
