#!/usr/bin/env bash
# task-360 一次性同步驗證，僅本場使用，勿當通用入口
set -euo pipefail

REPO="x812033727/Ti"
BRANCH="autopilot/task-360"
BRANCH_ENCODED="autopilot%2Ftask-360"
REMOTE_FILE="docs/evidence/release-v0.2.0-body-structure-verdict.json"
COMPARE_BASES=(baa6a9c 4d50a11 b7f3932)

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "找不到必要指令: $1"
}

need_cmd git
need_cmd gh
need_cmd base64
need_cmd grep

local_sha="$(git rev-parse "$BRANCH")" || fail "無法解析本地 ref: $BRANCH"
remote_sha="$(gh api "repos/${REPO}/branches/${BRANCH_ENCODED}" --jq .commit.sha)" \
  || fail "無法讀取遠端 branch: $BRANCH"

printf 'local_sha=%s\n' "$local_sha"
printf 'remote_sha=%s\n' "$remote_sha"

if [ "$local_sha" != "$remote_sha" ]; then
  fail "[a] 本地與遠端 sha 不一致"
fi
printf '[a] 本地/遠端 sha 相等: PASS\n'

for base in "${COMPARE_BASES[@]}"; do
  status="$(gh api "repos/${REPO}/compare/${base}...${BRANCH_ENCODED}" --jq .status)" \
    || fail "無法 compare ${base}...${BRANCH}"
  if [ "$status" != "ahead" ]; then
    fail "[b] compare ${base}...${BRANCH} status=${status}，預期 ahead"
  fi
  printf '[b] compare %s...%s status=%s: PASS\n' "$base" "$BRANCH" "$status"
done

remote_content_b64="$(
  gh api "repos/${REPO}/contents/${REMOTE_FILE}?ref=${BRANCH_ENCODED}" --jq .content
)" || fail "無法讀取遠端檔案: ${REMOTE_FILE}"
remote_content="$(printf '%s' "$remote_content_b64" | base64 --decode)" \
  || fail "遠端檔案 base64 解碼失敗: ${REMOTE_FILE}"

if grep -q 'body_sha256' <<<"$remote_content"; then
  fail "[c] 遠端檔案仍含 body_sha256: ${REMOTE_FILE}"
fi
printf '[c] 遠端檔案不含 body_sha256: PASS\n'

printf 'RESULT: PASS\n'
