#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REPO="${REPO:-x812033727/Ti}"
TAG="${TAG:-v0.2.0}"
TMP_ROOT="${TMPDIR:-/tmp}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="docs/evidence/release-v0.2.0-online-body.json"

for cmd in awk curl date gh jq sha256sum timeout; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "missing required command: $cmd" >&2
    exit 2
  }
done

preflight_log="$TMP_ROOT/task1_preflight_${STAMP}.log"
gh_raw="$TMP_ROOT/task1_gh_release_view_${TAG}_${STAMP}.json"
rest_raw="$TMP_ROOT/task1_rest_release_by_tag_${TAG}_${STAMP}.json"
compare_json="$TMP_ROOT/task1_identity_compare_${TAG}_${STAMP}.json"
evidence_identity="$TMP_ROOT/task1_evidence_identity_${TAG}_${STAMP}.json"
gh_identity="$TMP_ROOT/task1_gh_identity_${TAG}_${STAMP}.json"
rest_identity="$TMP_ROOT/task1_rest_identity_${TAG}_${STAMP}.json"
gh_identity_diff="$TMP_ROOT/task1_evidence_vs_gh_identity_${TAG}_${STAMP}.diff"
rest_identity_diff="$TMP_ROOT/task1_evidence_vs_rest_identity_${TAG}_${STAMP}.diff"
gh_err="$TMP_ROOT/task1_gh_release_view_${TAG}_${STAMP}.err"
rest_err="$TMP_ROOT/task1_rest_release_by_tag_${TAG}_${STAMP}.err"

run_preflight() {
  name="$1"
  shift
  {
    printf '[%s] %s\n' "$name" "$*"
    set +e
    timeout 60 "$@"
    code="$?"
    set -e
    printf '[%s] exit=%s\n' "$name" "$code"
    return "$code"
  } >>"$preflight_log" 2>&1
}

preflight_failed=0
run_preflight gh-auth-status gh auth status || preflight_failed=1
run_preflight network curl -sf https://api.github.com/rate_limit || preflight_failed=1
run_preflight release-readable gh release view "$TAG" --repo "$REPO" --json tagName,url || preflight_failed=1

if [ "$preflight_failed" != "0" ]; then
  jq -n \
    --arg status "BLOCKED" \
    --arg preflight_log "$preflight_log" \
    '{status:$status, preflight_log:$preflight_log}'
  exit 1
fi

timeout 60 gh release view "$TAG" --repo "$REPO" --json body,tagName,url >"$gh_raw" 2>"$gh_err"
timeout 60 gh api "repos/$REPO/releases/tags/$TAG" >"$rest_raw" 2>"$rest_err"

body_sha256() {
  jq -e -rj '.body // ""' "$1" | sha256sum | awk '{print $1}'
}

evidence_tag="$(jq -r '.gh_release_view.tagName' "$EVIDENCE")"
evidence_url="$(jq -r '.gh_release_view.url' "$EVIDENCE")"
evidence_body_sha256="$(jq -r '.body_sha256' "$EVIDENCE")"
gh_body_sha256="$(body_sha256 "$gh_raw")"
rest_body_sha256="$(body_sha256 "$rest_raw")"

jq -e -S '{
  tagName:.gh_release_view.tagName,
  url:.gh_release_view.url,
  body_sha256:.body_sha256
}' "$EVIDENCE" >"$evidence_identity"

jq -e -S --arg body_sha256 "$gh_body_sha256" '{
  tagName:.tagName,
  url:.url,
  body_sha256:$body_sha256
}' "$gh_raw" >"$gh_identity"

jq -e -S --arg body_sha256 "$rest_body_sha256" '{
  tagName:.tag_name,
  url:.html_url,
  body_sha256:$body_sha256
}' "$rest_raw" >"$rest_identity"

if diff -u "$evidence_identity" "$gh_identity" >"$gh_identity_diff"; then
  gh_identity_diff_status="MATCH"
else
  gh_identity_diff_status="MISMATCH"
fi

if diff -u "$evidence_identity" "$rest_identity" >"$rest_identity_diff"; then
  rest_identity_diff_status="MATCH"
else
  rest_identity_diff_status="MISMATCH"
fi

jq -n \
  --arg status "COMPARED" \
  --arg repo "$REPO" \
  --arg tag "$TAG" \
  --arg captured_at_utc "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg preflight_log "$preflight_log" \
  --arg gh_raw "$gh_raw" \
  --arg rest_raw "$rest_raw" \
  --arg compare_json "$compare_json" \
  --arg evidence_identity "$evidence_identity" \
  --arg gh_identity "$gh_identity" \
  --arg rest_identity "$rest_identity" \
  --arg gh_identity_diff "$gh_identity_diff" \
  --arg rest_identity_diff "$rest_identity_diff" \
  --arg gh_identity_diff_status "$gh_identity_diff_status" \
  --arg rest_identity_diff_status "$rest_identity_diff_status" \
  --arg evidence_path "$EVIDENCE" \
  --arg evidence_tag "$evidence_tag" \
  --arg evidence_url "$evidence_url" \
  --arg evidence_body_sha256 "$evidence_body_sha256" \
  --arg gh_tag "$(jq -r '.tagName' "$gh_raw")" \
  --arg gh_url "$(jq -r '.url' "$gh_raw")" \
  --arg gh_body_sha256 "$gh_body_sha256" \
  --arg rest_tag "$(jq -r '.tag_name' "$rest_raw")" \
  --arg rest_url "$(jq -r '.html_url' "$rest_raw")" \
  --arg rest_body_sha256 "$rest_body_sha256" \
  --argjson rest_id "$(jq -c '.id // null' "$rest_raw")" \
  --arg rest_created_at "$(jq -r '.created_at // ""' "$rest_raw")" \
  --arg rest_published_at "$(jq -r '.published_at // ""' "$rest_raw")" \
  '{
    status:$status,
    repo:$repo,
    tag:$tag,
    captured_at_utc:$captured_at_utc,
    raw_outputs:{gh_release_view:$gh_raw, rest_release_by_tag:$rest_raw},
    generated_outputs:{
      preflight_log:$preflight_log,
      compare_json:$compare_json,
      identity_extracts:{
        evidence:$evidence_identity,
        gh_release_view:$gh_identity,
        rest_release_by_tag:$rest_identity
      },
      identity_diffs:{
        evidence_vs_gh:{status:$gh_identity_diff_status, path:$gh_identity_diff},
        evidence_vs_rest:{status:$rest_identity_diff_status, path:$rest_identity_diff}
      }
    },
    evidence_path:$evidence_path,
    identity:{
      evidence:{tagName:$evidence_tag, url:$evidence_url, body_sha256:$evidence_body_sha256},
      gh_release_view:{tagName:$gh_tag, url:$gh_url, body_sha256:$gh_body_sha256},
      rest_release_by_tag:{tagName:$rest_tag, url:$rest_url, body_sha256:$rest_body_sha256}
    },
    comparisons:{
      tagName:{match:($evidence_tag == $gh_tag and $gh_tag == $rest_tag), evidence:$evidence_tag, gh:$gh_tag, rest:$rest_tag},
      url:{match:($evidence_url == $gh_url and $gh_url == $rest_url), evidence:$evidence_url, gh:$gh_url, rest:$rest_url},
      body_sha256:{match:($evidence_body_sha256 == $gh_body_sha256 and $gh_body_sha256 == $rest_body_sha256), evidence:$evidence_body_sha256, gh:$gh_body_sha256, rest:$rest_body_sha256}
    },
    volatile_recorded_not_compared:{
      rest_release_by_tag:{id:$rest_id, created_at:$rest_created_at, published_at:$rest_published_at}
    },
    note:"Identity mismatches are recorded here; this script exits 0 once fetch and comparison artifacts are produced."
  }' | tee "$compare_json"
