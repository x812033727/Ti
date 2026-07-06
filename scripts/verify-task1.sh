#!/usr/bin/env bash
set -euo pipefail

tmpdir="${TMPDIR:-/tmp}"
report="docs/release-e2e-closure-report.md"
online="docs/evidence/release-v0.2.0-online-body.json"
structure="docs/evidence/release-v0.2.0-body-structure-verdict.json"
smoke="docs/evidence/release-smoke-v0.2.0-trigger.json"

for cmd in jq diff rg wc; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "missing required command: $cmd" >&2
    exit 2
  }
done

evidence_json="$tmpdir/task1-evidence-identity.json"
report_json="$tmpdir/task1-report-identity.json"
evidence_fields="$tmpdir/task1-evidence-fields.tsv"
report_fields="$tmpdir/task1-report-fields.tsv"
identity_diff="$tmpdir/task1-identity.diff"
fields_diff="$tmpdir/task1-fields.diff"
summary_json="$tmpdir/task1-summary.json"

rg -n '^\| #[123] \|' "$report" >"$tmpdir/task1-report-table-lines.txt"
printf '%s\n' "$online" "$structure" "$smoke" >"$tmpdir/task1-input-files.txt"

jq -S -n \
  --arg online_path "$online" \
  --arg structure_path "$structure" \
  --arg smoke_path "$smoke" \
  --slurpfile online "$online" \
  --slurpfile structure "$structure" \
  --slurpfile smoke "$smoke" '
  def check_by($needle):
    .checks
    | to_entries
    | map(select(.key | contains($needle)))
    | .[0].value
    | tostring;

  [
    {
      row: "#1",
      evidence_path: $online_path,
      captured_at_utc: $online[0].captured_at_utc,
      body_sha256: $online[0].body_sha256,
      body_match: ($online[0].body_match | tostring),
      tag_match: ($online[0].tag_match | tostring),
      url_match: ($online[0].url_match | tostring)
    },
    {
      row: "#2",
      evidence_path: $structure_path,
      captured_at_utc: $structure[0].captured_at_utc,
      verdict: $structure[0].verdict,
      problems: ($structure[0].problems | tojson),
      source_match: ($structure[0] | check_by("gh vs REST")),
      breaking_top: ($structure[0] | check_by("Breaking")),
      four_elements: ($structure[0] | check_by("before/after")),
      effective_version: ($structure[0] | check_by("0.2.0")),
      escape_hatch: ($structure[0] | check_by("TI_REQUIRE_CHOWN"))
    },
    {
      row: "#3",
      evidence_path: $smoke_path,
      captured_at_utc: $smoke[0].captured_at_utc,
      run_id: $smoke[0].run_id,
      event: $smoke[0].event,
      status: $smoke[0].status,
      conclusion: $smoke[0].conclusion,
      workflow_path: $smoke[0].workflow_path
    }
  ]
' >"$evidence_json"

jq -R -s -S '
  def ticks: [match("`[^`]*`"; "g").string | gsub("^`|`$"; "")];
  def trim: gsub("^ +| +$"; "");
  def val($values; $index): ($values[$index] | split("=") | last);

  [
    split("\n")[]
    | select(test("^\\| #([123]) \\|"))
    | split("|") as $cols
    | {
        row: ($cols[1] | trim),
        evidence_path: (($cols[3] | ticks)[0]),
        captured_at_utc: (($cols[4] | ticks)[0]),
        key_values: ($cols[5] | ticks)
      }
    | if .row == "#1" then
        {
          row,
          evidence_path,
          captured_at_utc,
          body_sha256: val(.key_values; 0),
          body_match: val(.key_values; 1),
          tag_match: val(.key_values; 2),
          url_match: val(.key_values; 3)
        }
      elif .row == "#2" then
        {
          row,
          evidence_path,
          captured_at_utc,
          verdict: val(.key_values; 0),
          problems: val(.key_values; 1),
          source_match: val(.key_values; 2),
          breaking_top: val(.key_values; 3),
          four_elements: val(.key_values; 4),
          effective_version: val(.key_values; 5),
          escape_hatch: val(.key_values; 6)
        }
      elif .row == "#3" then
        {
          row,
          evidence_path,
          captured_at_utc,
          run_id: val(.key_values; 0),
          event: val(.key_values; 1),
          status: val(.key_values; 2),
          conclusion: val(.key_values; 3),
          workflow_path: val(.key_values; 4)
        }
      else
        empty
      end
  ]
' "$report" >"$report_json"

jq -r '.[] as $row | ($row | keys[]) as $key | [$row.row, $key, ($row[$key] | tostring)] | @tsv' "$evidence_json" >"$evidence_fields"
jq -r '.[] as $row | ($row | keys[]) as $key | [$row.row, $key, ($row[$key] | tostring)] | @tsv' "$report_json" >"$report_fields"

if diff -u "$evidence_json" "$report_json" >"$identity_diff"; then
  identity_status="PASS"
else
  identity_status="FAIL"
fi

if diff -u "$evidence_fields" "$report_fields" >"$fields_diff"; then
  fields_status="PASS"
else
  fields_status="FAIL"
fi

row_count="$(jq 'length' "$report_json")"
field_count="$(wc -l <"$evidence_fields" | tr -d ' ')"
result="PASS"
if [[ "$row_count" != "3" || "$identity_status" != "PASS" || "$fields_status" != "PASS" ]]; then
  result="FAIL"
fi

jq -n \
  --arg result "$result" \
  --arg identity_status "$identity_status" \
  --arg fields_status "$fields_status" \
  --argjson row_count "$row_count" \
  --argjson field_count "$field_count" \
  --arg evidence_json "$evidence_json" \
  --arg report_json "$report_json" \
  --arg identity_diff "$identity_diff" \
  --arg fields_diff "$fields_diff" \
  '{
    result: $result,
    row_count: $row_count,
    field_count: $field_count,
    identity_diff: $identity_status,
    fields_diff: $fields_status,
    outputs: {
      evidence_json: $evidence_json,
      report_json: $report_json,
      identity_diff: $identity_diff,
      fields_diff: $fields_diff
    }
  }' >"$summary_json"

if [[ "$result" != "PASS" ]]; then
  cat "$summary_json" >&2
  exit 1
fi

cat "$summary_json"
