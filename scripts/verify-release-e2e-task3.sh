#!/usr/bin/env bash
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT_DIR="${TMPDIR:-/tmp}"
PREFIX="$OUT_DIR/task3"
REPORT="docs/release-e2e-closure-report.md"
RUN_ID="27905531397"
REPO="x812033727/Ti"
RUN_VIEW_CMD="gh run view 27905531397 --json databaseId,event,status,conclusion,workflowName,url"
API_CMD="gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'"

run_capture() {
  name="$1"
  shift
  set +e
  timeout 60 "$@" >"$PREFIX-$name.out" 2>"$PREFIX-$name.err"
  code="$?"
  set -e
  printf '%s\n' "$code" >"$PREFIX-$name.exit"
  return "$code"
}

extract_report_value() {
  key="$1"
  grep -m1 -o "$key=[^、| ]*" "$REPORT" | sed "s/^$key=//; s/\`//g"
}

write_bool_from_exit() {
  file="$1"
  if [ "$(cat "$file")" = "0" ]; then
    printf 'true'
  else
    printf 'false'
  fi
}

compare_field() {
  field="$1"
  set +e
  diff -u "$PREFIX-report-$field.txt" "$PREFIX-actual-$field.txt" >"$PREFIX-diff-$field.txt"
  code="$?"
  set -e
  if [ "$code" = "0" ]; then
    printf '%s\tPASS\t0\n' "$field" >>"$PREFIX-diff-summary.tsv"
  else
    printf '%s\tFAIL\t%s\n' "$field" "$code" >>"$PREFIX-diff-summary.tsv"
  fi
  return "$code"
}

printf '%s\n' "$RUN_ID" >"$PREFIX-run-id.txt"
git status --porcelain docs/ >"$PREFIX-current-files.txt"

precheck_failed=0
run_capture precheck-gh-auth-status gh auth status || precheck_failed=1
run_capture precheck-network curl -sf https://api.github.com/rate_limit || precheck_failed=1
run_capture precheck-run-view gh run view "$RUN_ID" || precheck_failed=1

{
  printf 'gh_auth_status=%s\n' "$(cat "$PREFIX-precheck-gh-auth-status.exit")"
  printf 'network=%s\n' "$(cat "$PREFIX-precheck-network.exit")"
  printf 'run_view_readable=%s\n' "$(cat "$PREFIX-precheck-run-view.exit")"
} >"$PREFIX-precheck-exit-codes.txt"

if [ "$precheck_failed" != "0" ]; then
  printf 'FAIL\n' >"$PREFIX-summary.txt"
  jq -n \
    --argjson gh_auth_status "$(write_bool_from_exit "$PREFIX-precheck-gh-auth-status.exit")" \
    --argjson network "$(write_bool_from_exit "$PREFIX-precheck-network.exit")" \
    --argjson run_view_readable "$(write_bool_from_exit "$PREFIX-precheck-run-view.exit")" \
    '{pass:false, precheck:{gh_auth_status:$gh_auth_status, network:$network, run_view_readable:$run_view_readable}}' \
    >"$PREFIX-summary.json"
  exit 1
fi

anchor_count=0
grep -Fq '2026-07-06' "$REPORT" && anchor_count=$((anchor_count + 1))
grep -Fq 'run_id=27905531397' "$REPORT" && anchor_count=$((anchor_count + 1))
grep -Fq "$RUN_VIEW_CMD" "$REPORT" && anchor_count=$((anchor_count + 1))
grep -Fq "$API_CMD" "$REPORT" && anchor_count=$((anchor_count + 1))
printf '%s\n' "$anchor_count" >"$PREFIX-report-anchor-count.txt"

run_capture gh-run-view-json gh run view "$RUN_ID" --json databaseId,event,status,conclusion,workflowName,url
run_capture gh-api-run gh api "repos/$REPO/actions/runs/$RUN_ID" --jq '{id,event,status,conclusion,html_url,name,path}'

{
  printf 'gh_run_view_report_command=%s\n' "$(cat "$PREFIX-gh-run-view-json.exit")"
  printf 'gh_api_report_command=%s\n' "$(cat "$PREFIX-gh-api-run.exit")"
} >"$PREFIX-report-command-exit-codes.txt"

extract_report_value run_id >"$PREFIX-report-run_id.txt"
extract_report_value event >"$PREFIX-report-event.txt"
extract_report_value status >"$PREFIX-report-status.txt"
extract_report_value conclusion >"$PREFIX-report-conclusion.txt"
extract_report_value workflow_path >"$PREFIX-report-workflow_path.txt"

jq -r '.databaseId | tostring' "$PREFIX-gh-run-view-json.out" >"$PREFIX-actual-run_id.txt"
jq -r '.event' "$PREFIX-gh-run-view-json.out" >"$PREFIX-actual-event.txt"
jq -r '.status' "$PREFIX-gh-run-view-json.out" >"$PREFIX-actual-status.txt"
jq -r '.conclusion' "$PREFIX-gh-run-view-json.out" >"$PREFIX-actual-conclusion.txt"
jq -r '.path' "$PREFIX-gh-api-run.out" >"$PREFIX-actual-workflow_path.txt"

: >"$PREFIX-diff-summary.tsv"
diff_failed=0
compare_field run_id || diff_failed=1
compare_field event || diff_failed=1
compare_field status || diff_failed=1
compare_field conclusion || diff_failed=1
compare_field workflow_path || diff_failed=1

if [ "$anchor_count" = "4" ] && [ "$diff_failed" = "0" ]; then
  pass=true
  printf 'PASS\n' >"$PREFIX-summary.txt"
else
  pass=false
  printf 'FAIL\n' >"$PREFIX-summary.txt"
fi

jq -n \
  --argjson pass "$pass" \
  --argjson gh_auth_status "$(write_bool_from_exit "$PREFIX-precheck-gh-auth-status.exit")" \
  --argjson network "$(write_bool_from_exit "$PREFIX-precheck-network.exit")" \
  --argjson run_view_readable "$(write_bool_from_exit "$PREFIX-precheck-run-view.exit")" \
  --argjson report_recheck_anchor_pass "$([ "$anchor_count" = "4" ] && printf true || printf false)" \
  --argjson report_recheck_anchors "$anchor_count" \
  --argjson gh_run_view "$(cat "$PREFIX-gh-run-view-json.out")" \
  --argjson gh_api_run "$(cat "$PREFIX-gh-api-run.out")" \
  --rawfile diff_summary_tsv "$PREFIX-diff-summary.tsv" \
  --rawfile report_run_id "$PREFIX-report-run_id.txt" \
  --rawfile actual_run_id "$PREFIX-actual-run_id.txt" \
  --rawfile report_event "$PREFIX-report-event.txt" \
  --rawfile actual_event "$PREFIX-actual-event.txt" \
  --rawfile report_status "$PREFIX-report-status.txt" \
  --rawfile actual_status "$PREFIX-actual-status.txt" \
  --rawfile report_conclusion "$PREFIX-report-conclusion.txt" \
  --rawfile actual_conclusion "$PREFIX-actual-conclusion.txt" \
  --rawfile report_workflow_path "$PREFIX-report-workflow_path.txt" \
  --rawfile actual_workflow_path "$PREFIX-actual-workflow_path.txt" \
  --arg gh_run_view_cmd "$RUN_VIEW_CMD" \
  --arg gh_api_cmd "$API_CMD" \
  '{
    pass:$pass,
    precheck:{
      gh_auth_status:$gh_auth_status,
      network:$network,
      run_view_readable:$run_view_readable
    },
    report_recheck_anchor_pass:$report_recheck_anchor_pass,
    report_recheck_anchors:$report_recheck_anchors,
    report_commands_executed:{
      gh_run_view:$gh_run_view_cmd,
      gh_api:$gh_api_cmd
    },
    gh_run_view:$gh_run_view,
    gh_api_run:$gh_api_run,
    compared_fields:{
      run_id:{report:($report_run_id|rtrimstr("\n")), actual:($actual_run_id|rtrimstr("\n"))},
      event:{report:($report_event|rtrimstr("\n")), actual:($actual_event|rtrimstr("\n"))},
      status:{report:($report_status|rtrimstr("\n")), actual:($actual_status|rtrimstr("\n"))},
      conclusion:{report:($report_conclusion|rtrimstr("\n")), actual:($actual_conclusion|rtrimstr("\n"))},
      workflow_path:{report:($report_workflow_path|rtrimstr("\n")), actual:($actual_workflow_path|rtrimstr("\n"))}
    },
    diff_summary_tsv:$diff_summary_tsv
  }' >"$PREFIX-summary.json"

if [ "$pass" = true ]; then
  exit 0
fi
exit 1
