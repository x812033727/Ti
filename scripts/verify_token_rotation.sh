#!/usr/bin/env bash
# GH_PAT 輪替輔助工具：驗證新 token / 殘留 token 掃描 / 人工-AI 分界報表。
#
# 規格唯一權威：docs/token-rotation-runbook.md。本腳本「不」內嵌四項 PAT 規格文字，
# 只在 --report 指引人工回 runbook 核對，避免第二份 SSOT 漂移。
#
# 安全不變式（守門測試 tests/docs/test_qa_token_rotation_script.py 鎖定）：
#   token 明文單向流入、永不流出——全腳本零明文輸出路徑，禁用 `set -x`，
#   curl 只讀回 HTTP 狀態碼，殘留掃描只報「檔名+筆數」並遮蔽命中內容。
#
# exit code 契約：--report 恆 0；--scan 命中殘留回非 0（2）；--verify 依驗證結果。
set -uo pipefail

# 殘留 GitHub token 前綴（與 runbook 一致，全系列）：
#   ghp_（classic）/ gho_ ghs_ ghr_（OAuth/server/refresh）以 gh[posur]_ 涵蓋；
#   github_pat_（fine-grained）另列。
TOKEN_RE='gh[posur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}'

usage() {
  cat <<'EOF'
用法: verify_token_rotation.sh <--verify | --scan [目錄...] | --report>
  --verify   驗證新 GH_PAT 是否生效（需 $GH_PAT 在場；步驟 2b）
  --scan     殘留 token 掃描（預設 history/；可加傳 workspace 目錄）
  --report   輸出人工/AI 分界狀態表（恆 exit 0）
EOF
}

cmd_verify() {
  if [ -z "${GH_PAT:-}" ]; then
    echo "[verify] 未設定 \$GH_PAT：步驟 2b 需人工在場提供新 token 環境變數" >&2
    return 3
  fi
  if command -v gh >/dev/null 2>&1; then
    # 首選：綁定新 token 再驗，避免驗到 keyring 舊 token（假綠）。
    if GH_TOKEN="$GH_PAT" gh auth status >/dev/null 2>&1; then
      echo "[verify] gh 驗證（已綁定新 token）：PASS"
      echo "[verify] 提醒：PASS 僅證身分；scope 需人工回 runbook 核對四項規格"
      return 0
    fi
    echo "[verify] gh 驗證（已綁定新 token）：FAIL" >&2
    return 1
  fi
  # 無 gh CLI 時才退 curl；-o /dev/null 只讀回 HTTP 狀態碼，不印 token/回應主體。
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $GH_PAT" https://api.github.com/user 2>/dev/null || echo 000)"
  echo "[verify] curl /user HTTP: $code"
  if [ "$code" = "200" ]; then
    echo "[verify] 200 只證身分有效、不證 scope：需人工回 runbook 核對四項規格（Fine-grained / 只選本 repo / Contents RW / 到期日）才閉環"
    return 0
  fi
  echo "[verify] 非 200（$code）：token 無效或被撤" >&2
  return 1
}

cmd_scan() {
  local dirs=("$@")
  if [ "${#dirs[@]}" -eq 0 ]; then dirs=(history/); fi
  local targets=()
  local d
  for d in "${dirs[@]}"; do
    [ -e "$d" ] && targets+=("$d")
  done
  if [ "${#targets[@]}" -eq 0 ]; then
    echo "[scan] 無可掃描目標（傳入目錄不存在）：$*"
    return 0
  fi
  # 主工具：gitleaks 優先（--no-git 掃檔案系統含 untracked，內建 GitHub token 規則）。
  if command -v gitleaks >/dev/null 2>&1; then
    local hit=0
    for d in "${targets[@]}"; do
      if ! gitleaks detect --no-git --source "$d" >/dev/null 2>&1; then
        echo "[scan] gitleaks 命中殘留於: $d"
        hit=1
      fi
    done
    if [ "$hit" -ne 0 ]; then
      echo "[scan] 命中殘留 token，立即回 runbook 頂端重跑輪替" >&2
      return 2
    fi
    echo "[scan] gitleaks 未發現殘留 token（掃描: ${targets[*]}）"
    return 0
  fi
  # 零依賴 fallback：grep 全前綴；-l 只取檔名、-c 只取筆數，永不印命中行（零明文外洩）。
  local total=0
  local f n
  for d in "${targets[@]}"; do
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      n="$(grep -acE "$TOKEN_RE" "$f" 2>/dev/null || echo 0)"
      echo "[scan] 疑似殘留 token: $f（$n 筆；內容已遮蔽，請人工安全檢視）"
      total=$((total + 1))
    done < <(grep -rlE "$TOKEN_RE" "$d" 2>/dev/null || true)
  done
  if [ "$total" -ne 0 ]; then
    echo "[scan] 命中 $total 個檔含疑似殘留 token，立即回 runbook 頂端重跑輪替" >&2
    return 2
  fi
  echo "[scan] grep fallback 未發現殘留 token（掃描: ${targets[*]}）"
  return 0
}

cmd_report() {
  # 恆 exit 0；純狀態表，不呼叫 gh/curl、不觸碰任何明文。
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
- 步驟 2b 若走 curl，回 200 只證身分有效、不證 scope；需人工回 runbook 核對四項規格才閉環。
- 先發後撤：新 token 未通過 --verify 前，絕不撤舊（會 403 斷鏈）。
EOF
  return 0
}

main() {
  local sub="${1:-}"
  case "$sub" in
    --verify) shift; cmd_verify "$@"; return $? ;;
    --scan)   shift; cmd_scan "$@";   return $? ;;
    --report) shift; cmd_report "$@"; return $? ;;
    -h | --help | "") usage; return 0 ;;
    *) echo "未知子命令: $sub" >&2; usage; return 64 ;;
  esac
}

main "$@"
