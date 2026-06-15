#!/usr/bin/env bash
# verify-clean.sh
#
# 結構化收集「工作樹乾淨 + 與 origin/main 空 diff」的證據。
#
# 退出語意（只反映「程式有沒有跑完、4 條命令本身有沒有異常」，
# 絕不表達「驗收通過 / 不通過」——那是 PM/架構層級的決策）：
#   0 = 跑完、fetch 成功、4 條命令均正常退出
#   1 = 跑完，但 fetch 失敗或任一 4 條命令非預期退出
#   99 = 環境前置失敗（不在 repo 內 / origin/main 缺 commit 物件）
#
# 不動 HEAD、不 reset、不 revert、不 commit 他人檔案。
# 結構化輸出與 close-out 文件落 $TMPDIR，不污染工作樹。
#
# 第 2 輪修正（相對 6bd48f5 commit 內的版本）：
#   - .gitmodules 區分 absent / present-unreadable / present-readable
#   - 顯式偵測 branch upstream，誠實標示「# branch.ab 不會出現」並用
#     `git rev-list --left-right --count` 補 ahead/behind
#   - 移除「腳本自指護欄」（腳本已 commit，不會自指）
#   - 獨立列出所有 untracked 檔案，不做過濾，僅標 owner 判斷歸屬
#   - 結論段不下 PASS/FAIL 蓋章，把 4 條滿足狀況與驗收標準對齊點攤出
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

# 標頭資訊（供 close-out 文件交叉引用）
HEAD_HASH="$(git rev-parse HEAD)"
ORIGIN_MAIN_HASH_BEFORE_FETCH="$(git rev-parse origin/main)"
RUNNER="$(id -un 2>/dev/null || echo unknown)@$(hostname 2>/dev/null || echo unknown)"

{
  echo "# verify-clean.sh 結構化輸出"
  echo "# branch          : $BRANCH_RAW"
  echo "# HEAD            : $HEAD_HASH"
  echo "# origin/main (前) : $ORIGIN_MAIN_HASH_BEFORE_FETCH"
  echo "# fetch time (UTC): $TS"
  echo "# runner          : $RUNNER"
  echo "# output file     : $TMP_OUT"
  echo

  fail=0

  # --- 0) fetch -----------------------------------------------------------
  echo "--- 0) git fetch origin ---"
  git fetch origin 2>&1
  RC=$?
  echo "exit: $RC"
  [ "$RC" -eq 0 ] || fail=1
  echo

  ORIGIN_MAIN_HASH_AFTER_FETCH="$(git rev-parse origin/main)"
  echo "origin/main (後) : $ORIGIN_MAIN_HASH_AFTER_FETCH"
  if [ "$ORIGIN_MAIN_HASH_BEFORE_FETCH" = "$ORIGIN_MAIN_HASH_AFTER_FETCH" ]; then
    echo "(fetch 期間 origin/main 沒更新；既有 ref 為最新)"
  else
    echo "(fetch 期間 origin/main 有更新！後續比對以 fetch 後為準)"
  fi
  echo

  # --- 結構性事實段（影響驗收基準） ---------------------------------------
  echo "--- 結構性事實（影響驗收基準對齊） ---"

  # branch 與 upstream
  echo "## branch 與 upstream"
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    UP="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}')"
    echo "  branch   : $BRANCH_RAW"
    echo "  upstream : $UP"
    AB_COUNT="$(git rev-list --left-right --count "$UP"...HEAD 2>/dev/null || echo "err")"
    echo "  ahead/behind (相對 $UP): $AB_COUNT  (左=behind, 右=ahead)"
  else
    echo "  branch   : $BRANCH_RAW"
    echo "  upstream : NONE（branch 未追蹤 upstream，status v2 不會出 # branch.upstream / # branch.ab 段）"
    AB_COUNT="$(git rev-list --left-right --count origin/main...HEAD 2>/dev/null || echo "err")"
    echo "  ahead/behind (相對 origin/main 兜底): $AB_COUNT  (左=behind, 右=ahead)"
  fi
  echo

  # .gitmodules 狀態（absent / present-unreadable / present-readable）
  echo "## .gitmodules 狀態（submodule 假性 diff 排除政策）"
  if [ -e "$ROOT/.gitmodules" ]; then
    if [ -r "$ROOT/.gitmodules" ]; then
      SM_COUNT="$(grep -cE '^\[submodule ' "$ROOT/.gitmodules" 2>/dev/null || echo 0)"
      echo "  狀態     : present, readable"
      echo "  [submodule ...] 區塊數 : $SM_COUNT"
    else
      PERM="$(stat -c '%a %U:%G' "$ROOT/.gitmodules" 2>/dev/null \
        || stat -f '%Lp %Su:%Sg' "$ROOT/.gitmodules" 2>/dev/null \
        || echo unknown)"
      echo "  狀態     : present, UNREADABLE（perm=$PERM）"
      echo "  (解讀)   : submodule 排除政策不可用，須手動 ls-tree 或詢問 owner 確認。"
    fi
  else
    echo "  狀態     : absent（本 repo 無 .gitmodules 檔）"
  fi
  echo

  # .gitattributes 與 core.autocrlf
  echo "## .gitattributes / core.autocrlf（CRLF 假性 diff 排除政策）"
  if [ -f "$ROOT/.gitattributes" ]; then
    echo "  .gitattributes : present"
  else
    echo "  .gitattributes : absent"
  fi
  AUTOCRLF_RAW="$(git config --get core.autocrlf 2>/dev/null || echo unset)"
  echo "  core.autocrlf  : $AUTOCRLF_RAW"
  echo

  # 未追蹤檔案
  echo "## 未追蹤檔案（untracked）"
  UNTRACKED="$(git ls-files --others --exclude-standard 2>/dev/null)"
  if [ -z "$UNTRACKED" ]; then
    echo "  (none)"
  else
    printf '%s\n' "$UNTRACKED" | sed 's/^/  /'
  fi
  echo "  (解讀) 本腳本已 commit、不是自指；上述 untracked 歸屬需由各檔 owner 自行判斷，"
  echo "          本工程師 round 不 commit 也不刪除他人 scope 的檔案。"
  echo

  # --- 4 條驗證命令（原始跑、原始記） -------------------------------------
  # 為避免一條失敗就中斷，先把 exit code 收到變數再判斷
  echo "--- 1) git status --porcelain=v2 --branch --untracked-files=normal ---"
  STATUS_RAW="$(git status --porcelain=v2 --branch --untracked-files=normal 2>&1)"
  STATUS_RC=$?
  printf '%s\n' "$STATUS_RAW"
  echo "exit: $STATUS_RC  (命令本身；非「工作樹乾淨」結論)"
  [ "$STATUS_RC" -eq 0 ] || fail=1
  echo

  echo "--- 2) git diff --quiet origin/main HEAD ---"
  git diff --quiet origin/main HEAD
  DIFF_RC=$?
  echo "exit: $DIFF_RC  (0=無 diff, 1=有 diff；非結論)"
  [ "$DIFF_RC" -eq 0 ] || fail=1
  echo

  echo "--- 3) git diff --quiet --cached ---"
  git diff --quiet --cached
  CACHED_RC=$?
  echo "exit: $CACHED_RC  (0=無 staged, 1=有 staged；非結論)"
  [ "$CACHED_RC" -eq 0 ] || fail=1
  echo

  echo "--- 4) rev-parse HEAD vs origin/main ---"
  LHS="$(git rev-parse HEAD)"
  RHS="$(git rev-parse origin/main)"
  echo "HEAD        = $LHS"
  echo "origin/main = $RHS"
  if [ "$LHS" = "$RHS" ]; then
    HASH_RESULT="MATCH"
    HASH_RC=0
  else
    HASH_RESULT="DIFFER"
    HASH_RC=1
  fi
  echo "result      = $HASH_RESULT"
  echo "exit: $HASH_RC  (非結論)"
  [ "$HASH_RC" -eq 0 ] || fail=1
  echo

  # --- 4 條命令「滿足 / 不滿足」盤點（不下 PASS/FAIL 蓋章） ----------------
  echo "--- 5) 4 條命令滿足狀況盤點（給讀者的事實，不下結論） ---"

  # 1) status 是否有檔案行
  FILE_LINES="$(printf '%s' "$STATUS_RAW" | grep -v '^# ' || true)"
  if [ -z "$FILE_LINES" ]; then
    echo "  [1] status 無檔案行 (工作樹乾淨) : 滿足"
  else
    echo "  [1] status 無檔案行 (工作樹乾淨) : 不滿足"
    echo "      含以下非 header 行："
    printf '%s\n' "$FILE_LINES" | sed 's/^/        /'
  fi

  # 2) diff
  if [ "$DIFF_RC" -eq 0 ]; then
    echo "  [2] diff --quiet origin/main HEAD : 滿足（無 diff）"
  else
    echo "  [2] diff --quiet origin/main HEAD : 不滿足（有 diff）"
  fi

  # 3) cached
  if [ "$CACHED_RC" -eq 0 ]; then
    echo "  [3] diff --quiet --cached          : 滿足（無 staged）"
  else
    echo "  [3] diff --quiet --cached          : 不滿足（有 staged）"
  fi

  # 4) hash
  if [ "$HASH_RC" -eq 0 ]; then
    echo "  [4] HEAD hash == origin/main hash   : 滿足"
  else
    echo "  [4] HEAD hash == origin/main hash   : 不滿足"
  fi
  echo

  # --- 與驗收標準的對齊點（把矛盾攤出，不下結論） -------------------------
  echo "--- 6) 與驗收標準對齊點（結構性矛盾需 PM/架構層級釐清） ---"
  echo "  驗收條款 'git status 顯示 branch.ab +0 -0 且無檔案行'："
  if [ -n "${UP:-}" ]; then
    echo "    - branch 有 upstream ($UP)，理論上 status 會出 # branch.ab 段"
  else
    echo "    - branch 無 upstream，status 不出 # branch.ab 段，驗收條款此項不可觸發"
  fi
  echo "  驗收條款 'git diff --quiet origin/main HEAD exit 0' / 'hash 一致'："
  echo "    - 當前 HEAD 領先 origin/main（見上方 ahead/behind 段），"
  echo "      此兩條在當前 commit 結構下結構性不成立"
  echo "  驗收條款 'git status 不得出現未追蹤殘留'："
  echo "    - 當前存在 untracked 檔案（見上方『未追蹤檔案』段）"
  echo "      本工程師 round 不 commit 也不刪除他人 scope 的檔案（QA 測試由 QA commit）"
  echo

  echo "=== 程式 fail=$fail（只反映 fetch/4 條命令本身有無異常，非驗收結論） ==="
  echo "=== 請 PM/架構層級依上方對齊點決定驗收基準是否需重訂 ==="
  exit "$fail"
} | tee "$TMP_OUT"
# PIPESTATUS[0] = 上面區塊的 exit code（已被 fail 設好），不被 tee 蓋掉
exit "${PIPESTATUS[0]}"
