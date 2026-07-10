#!/usr/bin/env bash
# GH_PAT 輪替輔助工具：驗證新 token / 殘留 token 掃描 / 人工-AI 分界報表。
#
# 規格唯一權威：docs/token-rotation-runbook.md。本腳本不內嵌四項 PAT 規格文字，
# 只在 --report 指引人工回 runbook 核對，避免第二份 SSOT 漂移。
set -uo pipefail

if [[ $- == *x* ]]; then
  set +x
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN_RE='gh[posur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}'

usage() {
  cat <<'EOF'
用法: bash scripts/verify_token_rotation.sh <--verify | --scan [目錄...] | --report>
  --verify   驗證新 GH_PAT 是否生效；首選 GH_TOKEN="$GH_PAT" gh auth status
  --scan     殘留 token 掃描；gitleaks --no-git 優先，無法安全遮蔽時退 grep fallback
  --report   輸出人工/AI 分界狀態表（恆 exit 0）
EOF
}

load_gh_pat() {
  if [ -n "${GH_PAT:-}" ]; then
    return 0
  fi

  local env_file="${TOKEN_ROTATION_ENV_FILE:-${ROOT_DIR}/.env}"
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

cmd_verify() {
  if ! load_gh_pat; then
    echo "[verify] 未設定 \$GH_PAT：步驟 2b 需人工在場提供新 token 環境變數" >&2
    return 3
  fi

  if command -v gh >/dev/null 2>&1; then
    # 綁定新 token 再驗：GH_TOKEN="$GH_PAT" gh auth status，避免驗到 keyring 舊值。
    if GH_TOKEN="$GH_PAT" gh auth status >/dev/null 2>&1; then
      echo "[verify] gh 驗證已綁定 GH_TOKEN：PASS"
      echo "[verify] 提醒：PASS 僅證身分；scope 需人工回 runbook 核對四項規格"
      return 0
    fi
    echo "[verify] gh 驗證已綁定 GH_TOKEN：FAIL" >&2
    return 1
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "[verify] 找不到 gh 或 curl，無法驗證" >&2
    return 3
  fi

  local http_code
  http_code="$(
    printf 'Authorization: Bearer %s\n' "$GH_PAT" \
      | curl -sS --max-time 20 -o /dev/null -w '%{http_code}' -H @- https://api.github.com/user 2>/dev/null
  )"
  if [ -z "$http_code" ]; then
    http_code="000"
  fi

  echo "[verify] curl /user HTTP: $http_code"
  if [ "$http_code" = "200" ]; then
    echo "[verify] 200 只證身分有效、不證 scope；需人工回 runbook 核對四項規格才閉環"
    return 0
  fi

  echo "[verify] 非 200（$http_code）：token 無效、權限異常或已撤銷" >&2
  return 1
}

default_scan_targets() {
  if [ -d "${ROOT_DIR}/history" ]; then
    printf '%s\n' "${ROOT_DIR}/history"
  fi
  printf '%s\n' "$ROOT_DIR"
}

gitleaks_can_redact() {
  command -v gitleaks >/dev/null 2>&1 \
    && gitleaks detect --help 2>/dev/null | grep -q -- '--redact'
}

scan_with_gitleaks() {
  local target="$1"
  if gitleaks detect --no-git --redact --source "$target" >/dev/null 2>&1; then
    return 0
  fi
  return 2
}

scan_with_grep() {
  local target="$1"
  local files=()
  local file count

  while IFS= read -r file; do
    [ -n "$file" ] && files+=("$file")
  done < <(
    grep -IRlE \
      --exclude-dir=.git \
      --exclude-dir=.venv \
      --exclude-dir=node_modules \
      --exclude-dir=__pycache__ \
      "$TOKEN_RE" \
      -- "$target" 2>/dev/null || true
  )

  if [ "${#files[@]}" -eq 0 ]; then
    return 0
  fi

  for file in "${files[@]}"; do
    count="$(grep -IEo "$TOKEN_RE" -- "$file" 2>/dev/null | wc -l | tr -d '[:space:]')"
    [ -n "$count" ] || count=0
    echo "[scan] 疑似殘留 token: $file（$count 筆；內容已遮蔽，請人工安全檢視）"
  done
  return 2
}

cmd_scan() {
  local input_targets=("$@")
  local targets=()
  local target result failures=0 scanned=0

  if [ "${#input_targets[@]}" -eq 0 ]; then
    mapfile -t input_targets < <(default_scan_targets)
  fi

  for target in "${input_targets[@]}"; do
    if [ -e "$target" ]; then
      targets+=("$target")
    else
      echo "[scan] skip 不存在目標: $target"
    fi
  done

  if [ "${#targets[@]}" -eq 0 ]; then
    echo "[scan] 無可掃描目標"
    return 0
  fi

  if gitleaks_can_redact; then
    for target in "${targets[@]}"; do
      scanned=1
      if scan_with_gitleaks "$target"; then
        echo "[scan] gitleaks 未發現殘留 token（掃描: $target）"
      else
        echo "[scan] gitleaks 命中疑似殘留 token: $target（輸出已遮蔽）" >&2
        failures=1
      fi
    done
  else
    if command -v gitleaks >/dev/null 2>&1; then
      echo "[scan] gitleaks 無可確認的 --redact 支援，改用 grep fallback 避免明文輸出"
    else
      echo "[scan] gitleaks not found; using grep fallback"
    fi
    for target in "${targets[@]}"; do
      scanned=1
      result=0
      scan_with_grep "$target" || result=$?
      if [ "$result" -eq 0 ]; then
        echo "[scan] grep fallback 未發現殘留 token（掃描: $target）"
      else
        echo "[scan] grep fallback 命中疑似殘留 token: $target" >&2
        failures=1
      fi
    done
  fi

  if [ "$scanned" -eq 0 ]; then
    echo "[scan] 無可掃描目標"
    return 0
  fi
  if [ "$failures" -ne 0 ]; then
    echo "[scan] 命中殘留 token，立即回 runbook 頂端重跑輪替" >&2
    return 2
  fi
  return 0
}

cmd_report() {
  cat <<'EOF'
== GH_PAT 輪替 人工/AI 分界狀態表 ==
規格唯一權威：docs/token-rotation-runbook.md（本表不內嵌四項 PAT 規格）

步驟 | 動作                                     | 誰做      | 狀態
-----+------------------------------------------+-----------+----------------------
1    | 產生新 fine-grained PAT（勾權限/複製明文） | 人工      | 待人工（GitHub UI）
2a   | 更新 .env + 同名 repo secret GH_PAT       | 人工      | 待人工（明文寫入）
2b   | 驗證新 token 生效（--verify）             | AI 可代勞 | 需 $GH_PAT 在場
3    | 到 UI Delete 舊 token                     | 人工      | 待人工（不可逆/無 API）
掃描 | 殘留 token 掃描（--scan）                 | AI 可代勞 | AI 執行

明示事項：
- 步驟 1（發新）與步驟 3（撤舊）待人工於 GitHub UI 完成，AI 不代行。
- 步驟 2b 首選 GH_TOKEN="$GH_PAT" gh auth status；若走 curl，回 200 只證身分有效、不證 scope；需人工回 runbook 核對四項規格才閉環。
- 先發後撤：新 token 未通過 --verify 前，絕不撤舊（會 403 斷鏈）。
EOF
  return 0
}

main() {
  local sub="${1:-}"
  case "$sub" in
    --verify)
      shift
      cmd_verify "$@"
      ;;
    --scan)
      shift
      cmd_scan "$@"
      ;;
    --report)
      shift
      if [ "$#" -ne 0 ]; then
        usage >&2
        return 64
      fi
      cmd_report
      ;;
    -h|--help|"")
      usage
      ;;
    *)
      echo "未知子命令: $sub" >&2
      usage >&2
      return 64
      ;;
  esac
}

main "$@"
