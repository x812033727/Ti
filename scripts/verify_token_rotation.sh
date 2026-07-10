#!/usr/bin/env bash
set -euo pipefail
if [[ $- == *x* ]]; then
  set +x
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITHUB_TOKEN_REGEX='(ghp_[A-Za-z0-9]{36,}|gho_[A-Za-z0-9]{36,}|ghs_[A-Za-z0-9]{36,}|ghr_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})'

usage() {
  cat <<'EOF'
Usage:
  bash scripts/verify_token_rotation.sh --verify
  bash scripts/verify_token_rotation.sh --scan [path ...]
  bash scripts/verify_token_rotation.sh --report

Commands:
  --verify  Verify GH_PAT by running GH_TOKEN="$GH_PAT" gh auth status.
            If gh is unavailable, fall back to curl /user and require HTTP 200.
  --scan    Scan residual GitHub tokens. Uses gitleaks --no-git when available;
            otherwise uses grep fallback for ghp_/github_pat_/gho_/ghs_/ghr_.
  --report  Print the manual/AI boundary table for GH_PAT rotation.
EOF
}

load_gh_pat() {
  if [ -n "${GH_PAT:-}" ]; then
    return 0
  fi

  local env_file="${ROOT_DIR}/.env"
  if [ ! -r "$env_file" ]; then
    return 1
  fi

  local line value
  line="$(grep -m1 -E '^(export[[:space:]]+)?GH_PAT=' "$env_file" || true)"
  if [ -z "$line" ]; then
    return 1
  fi

  value="${line#export }"
  value="${value#GH_PAT=}"
  value="${value%$'\r'}"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi

  if [ -z "$value" ]; then
    return 1
  fi

  export GH_PAT="$value"
}

verify_token() {
  if ! load_gh_pat; then
    echo "FAIL: GH_PAT is not set in environment or readable .env" >&2
    exit 2
  fi

  if command -v gh >/dev/null 2>&1; then
    echo "== verify: gh CLI with explicit GH_TOKEN binding =="
    GH_TOKEN="$GH_PAT" gh auth status
    echo 'PASS: GH_TOKEN="$GH_PAT" gh auth status accepted GH_PAT binding'
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "FAIL: neither gh nor curl is available" >&2
    exit 2
  fi

  echo "== verify: curl fallback against https://api.github.com/user =="
  local http_code
  http_code="$(
    printf 'Authorization: Bearer %s\n' "$GH_PAT" \
      | curl -sS --max-time 20 -o /dev/null -w '%{http_code}' -H @- https://api.github.com/user
  )"

  if [ "$http_code" = "200" ]; then
    echo "PASS: curl returned HTTP 200"
    echo "NOTE: HTTP 200 proves identity only; manually confirm runbook scope requirements."
    return 0
  fi

  echo "FAIL: curl returned HTTP ${http_code}" >&2
  return 1
}

default_scan_targets() {
  if [ -d "${ROOT_DIR}/history" ]; then
    printf '%s\n' "${ROOT_DIR}/history"
  fi
  printf '%s\n' "$ROOT_DIR"
}

redact_github_tokens() {
  local line="$1"
  local match
  while [[ "$line" =~ $GITHUB_TOKEN_REGEX ]]; do
    match="${BASH_REMATCH[0]}"
    line="${line/"$match"/[REDACTED_GITHUB_TOKEN]}"
  done
  printf '%s\n' "$line"
}

redacted_grep_scan_one() {
  local target="$1"
  local grep_args=(
    -IRnE
    --exclude-dir=.git
    --exclude-dir=.venv
    --exclude-dir=node_modules
    --exclude-dir=__pycache__
    "$GITHUB_TOKEN_REGEX"
    --
    "$target"
  )

  if grep "${grep_args[@]}" >/dev/null 2>&1; then
    while IFS= read -r line; do
      redact_github_tokens "$line"
    done < <(grep "${grep_args[@]}")
    return 1
  fi

  return 0
}

gitleaks_can_redact() {
  command -v gitleaks >/dev/null 2>&1 && gitleaks detect --help 2>/dev/null | grep -q -- '--redact'
}

scan_one_target() {
  local target="$1"
  if [ ! -e "$target" ]; then
    echo "FAIL: scan target not found: ${target}" >&2
    return 2
  fi

  echo "== scan target: ${target} =="
  if gitleaks_can_redact; then
    if gitleaks detect --no-git --redact --source "$target"; then
      echo "PASS: gitleaks found no residual GitHub token"
      return 0
    fi
    echo "FAIL: gitleaks reported potential secret(s); output above is redacted" >&2
    return 1
  fi

  if command -v gitleaks >/dev/null 2>&1; then
    echo "INFO: gitleaks is installed but lacks --redact; using grep fallback to avoid secret output"
  else
    echo "INFO: gitleaks not found; using grep fallback"
  fi

  if redacted_grep_scan_one "$target"; then
    echo "PASS: grep fallback found no residual GitHub token"
    return 0
  fi

  echo "FAIL: grep fallback found potential GitHub token(s); output above is redacted" >&2
  return 1
}

scan_targets() {
  local targets=("$@")
  if [ "${#targets[@]}" -eq 0 ]; then
    mapfile -t targets < <(default_scan_targets)
  fi

  local failures=0
  local target
  for target in "${targets[@]}"; do
    if ! scan_one_target "$target"; then
      failures=1
    fi
  done

  if [ "$failures" -ne 0 ]; then
    return 1
  fi
  return 0
}

report_status() {
  cat <<'EOF'
# GH_PAT Token 輪替狀態

| 步驟 | 誰做 | 狀態 | 分界 |
|------|------|------|------|
| 1. 發新 fine-grained PAT | 人工 | 待人工 | 到 GitHub UI 產生；token 明文不可進對話、log 或 git。 |
| 1/2. 更新 repo secret GH_PAT 與本機 .env | 人工 | 待人工 | 明文只寫入 GitHub Actions secret 與本機/部署環境。 |
| 2. 驗證新 token | AI 可代勞 | 可執行 --verify | 腳本使用 GH_TOKEN="$GH_PAT" gh auth status；無 gh 時 curl /user 必須回 200。 |
| 3. 撤銷舊 token | 人工 | 待人工 | 只可在步驟 2 通過後，到 GitHub UI 刪除舊 fine-grained PAT。 |
| 掃描. 殘留 token 掃描 | AI 可代勞 | 可執行 --scan | gitleaks --no-git 優先；grep fallback 涵蓋 ghp_/github_pat_/gho_/ghs_/ghr_ 並遮蔽命中。 |

scope 注意：curl HTTP 200 只證明身分有效，不證 repository scope。需人工核對 runbook 四項 GH_PAT 規格：fine-grained token、只選本 repo、Contents: Read and write、secret 名稱 GH_PAT。
順序鎖定：發新 -> 更新 .env 與 repo secret -> 驗證 -> 撤舊；不得先撤。
EOF
}

main() {
  if [ "$#" -lt 1 ]; then
    usage >&2
    exit 2
  fi

  local command="$1"
  shift

  case "$command" in
    --verify)
      if [ "$#" -ne 0 ]; then
        usage >&2
        exit 2
      fi
      verify_token
      ;;
    --scan)
      scan_targets "$@"
      ;;
    --report)
      if [ "$#" -ne 0 ]; then
        usage >&2
        exit 2
      fi
      report_status
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
