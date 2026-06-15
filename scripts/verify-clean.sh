#!/usr/bin/env bash
# verify-clean.sh
# 驗證「工作樹乾淨 + 與 origin/main 空 diff」並結構化收集證據。
#
# 退出語意：
#   0 = 4 條驗證命令全部 exit 0 且 fetch 成功
#   1 = fetch 失敗，或任一 4 條非預期退出
#   99 = 環境前置失敗（不在 repo 內 / origin/main 缺 commit 物件）
#
# 不動 HEAD、不 reset、不 revert —— 本腳本只驗證不修補。
# 所有產物（結構化輸出 + 稍後 PM 寫的 close-out 文件）均落 $TMPDIR，
# 不污染工作樹，符合驗收條款「git status 不得出現未追蹤殘留」。
set -u

# --- 環境前置 --------------------------------------------------------------
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 99

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[FATAL] not inside a git work tree ($ROOT)"; exit 99
fi
if ! git rev-parse --verify origin/main^{commit} >/dev/null 2>&1; then
  echo "[FATAL] origin/main 沒有可解析的 commit（先 git fetch origin）"; exit 99
fi

# --- 輸出檔路徑（不入版控、不污染工作樹） -----------------------------------
BRANCH_RAW="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo DETACHED)"
BRANCH_SAFE="$(printf '%s' "$BRANCH_RAW" | tr '/\\' '__')"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_OUT="${TMPDIR:-/tmp}/clean-verify-output-${BRANCH_SAFE}-${TS}.txt"

# 記錄標頭（供 close-out 文件交叉引用，零成本）
HEAD_HASH="$(git rev-parse HEAD)"
ORIGIN_MAIN_HASH="$(git rev-parse origin/main)"
RUNNER="$(id -un 2>/dev/null || echo unknown)@$(hostname 2>/dev/null || echo unknown)"

{
  echo "# verify-clean.sh 結構化輸出"
  echo "# branch         : $BRANCH_RAW"
  echo "# HEAD           : $HEAD_HASH"
  echo "# origin/main    : $ORIGIN_MAIN_HASH"
  echo "# fetch time (UTC): $TS"
  echo "# runner         : $RUNNER"
  echo "# output file    : $TMP_OUT"
  echo

  fail=0

  # --- 0) fetch -----------------------------------------------------------
  echo "--- 0) git fetch origin ---"
  git fetch origin 2>&1
  RC=$?
  echo "exit: $RC"
  [ "$RC" -eq 0 ] || fail=1
  echo

  # --- 假性 diff 排除政策偵測（不修補，僅記錄）----------------------------
  echo "--- 假性 diff 排除政策偵測（為何本 repo 不受影響） ---"

  # .gitmodules
  if [ -f "$ROOT/.gitmodules" ]; then
    SM_LINES=$(grep -cE '^\[submodule ' "$ROOT/.gitmodules" 2>/dev/null || echo 0)
    echo ".gitmodules   : present, [submodule ...] count = $SM_LINES"
  else
    echo ".gitmodules   : absent"
  fi

  # .gitattributes
  if [ -f "$ROOT/.gitattributes" ]; then
    echo ".gitattributes : present"
  else
    echo ".gitattributes : absent"
  fi

  # core.autocrlf
  AUTOCRLF_RAW="$(git config --get core.autocrlf 2>/dev/null || echo unset)"
  echo "core.autocrlf  : $AUTOCRLF_RAW"
  echo

  # --- 1) status --porcelain=v2 --branch ----------------------------------
  echo "--- 1) git status --porcelain=v2 --branch --untracked-files=normal ---"
  STATUS_RAW="$(git status --porcelain=v2 --branch --untracked-files=normal 2>&1)"
  RC=$?
  printf '%s\n' "$STATUS_RAW"
  echo "exit: $RC"
  # 自指護欄：本腳本本身是新增的 untracked 檔，會以 "? scripts/verify-clean.sh" 形式出現；
  # 這是腳本自指造成的、不是工作樹 dirty 證據。close-out 文件需對應標示。
  if printf '%s' "$STATUS_RAW" | grep -q '^? scripts/verify-clean\.sh$'; then
    echo "(解讀) status 段含 untracked 行 ? scripts/verify-clean.sh —— 為本腳本自指，"
    echo "        非工作樹 dirty 證據；其餘行（含 # branch.oid / branch.head）才是判定來源。"
  fi
  [ "$RC" -eq 0 ] || fail=1
  echo

  # --- 2) diff --quiet origin/main HEAD -----------------------------------
  echo "--- 2) git diff --quiet origin/main HEAD ---"
  git diff --quiet origin/main HEAD
  RC=$?
  echo "exit: $RC  (0=無 diff, 1=有 diff, 預期 0)"
  [ "$RC" -eq 0 ] || fail=1
  echo

  # --- 3) diff --quiet --cached -------------------------------------------
  echo "--- 3) git diff --quiet --cached ---"
  git diff --quiet --cached
  RC=$?
  echo "exit: $RC  (0=無 staged diff, 1=有 staged diff, 預期 0)"
  [ "$RC" -eq 0 ] || fail=1
  echo

  # --- 4) hash 比對 ------------------------------------------------------
  echo "--- 4) rev-parse HEAD vs origin/main ---"
  LHS="$(git rev-parse HEAD)"
  RHS="$(git rev-parse origin/main)"
  echo "HEAD        = $LHS"
  echo "origin/main = $RHS"
  if [ "$LHS" = "$RHS" ]; then
    echo "result      = MATCH"
    RC=0
  else
    echo "result      = DIFFER"
    RC=1
  fi
  echo "exit: $RC  (預期 0)"
  [ "$RC" -eq 0 ] || fail=1
  echo

  echo "=== 總體 fail=$fail ==="
  exit "$fail"
} | tee "$TMP_OUT"
# PIPESTATUS[0] = 腳本聚合區塊的 exit code（已被總體 fail=$fail 設好），
# 不會被 tee 蓋掉；不依賴 pipefail（architect 決策已否決）。
exit "${PIPESTATUS[0]}"
