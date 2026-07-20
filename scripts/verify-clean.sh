#!/usr/bin/env bash
# verify-clean.sh — 架構師定案 v2 整合版（task-4 重新交付）
#
# 與上輪 v7 的差異（v2 三項實質修正）:
#   (a) .gitmodules 改為「實讀內容 + 解析 [submodule "..."] 區塊數」
#       讀失敗 (空檔 / 字元裝置 / permission denied) 時
#       fallback 為「非常規檔 = 無 submodule」並附 ls -la 為證
#   (b) 輸出檔名加入 sanitized branch name（去 / 反斜線）
#   (c) fetch 失敗時 close-out 標頭顯眼標示「fetch 失敗, 比對結果作廢」
#       fetch 失敗 → fail=1,因 origin/main 為過時 ref 後續比對不可信
#   保留:worktree 機制（origin/main 視角）+ lane 端實況（sandbox 視角）+
#        Step 9 結論分組 + 三行組輸出
#
# 退出語意:
#   0 = 程式跑完、4 條命令全 exit 0、fetch 成功
#   1 = 跑完但有異常（fetch 失敗 / 4 條任一 exit 1 / worktree 綁定失敗）
#   99 = 環境前置失敗
#   exit code 不代表「驗收通過/不通過」,只代表「4 條命令本身 + 環境有無異常」
#
# 驗證對象（close-out 標頭明示,防假綠）:
#   1) published origin/main（via detached worktree）= release gate 視角
#   2) sandbox lane HEAD（直接跑）              = 審計視角
#
# 對齊 in-repo precedent: scripts/baseline_selftest.sh（set -u + 逐條 RC 累計）

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 99

# --- 路徑規劃（branch name 進入 filename,去 / 反斜線）-----------------
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LANE_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
SANITIZED_BRANCH="$(printf '%s' "$LANE_BRANCH" | tr '/\\' '__')"
TMP_BASE="${TMPDIR:-/tmp}"
ARTIFACT_DIR="${TMP_BASE}/verify-clean-artifacts"
mkdir -p "$ARTIFACT_DIR" || exit 99

cleanup_stale_artifacts() {
  for stale in \
    "$TMP_BASE"/clean-verify-output-*.txt \
    "$TMP_BASE"/git-warnings-*.log
  do
    [ -e "$stale" ] || continue
    rm -f -- "$stale"
  done
  find "$ARTIFACT_DIR" -maxdepth 1 -type f \( \
    -name 'clean-verify-output-*.txt' -o \
    -name 'git-warnings-*.log' -o \
    -name 'manifest-*.env' \
  \) -mtime +7 -exec rm -f -- {} + 2>/dev/null || true
}

cleanup_stale_artifacts

RUN_ID="${SANITIZED_BRANCH}-${TS}-$$"
OUT_FILE="${ARTIFACT_DIR}/clean-verify-output-${RUN_ID}.txt"
WARN_FILE="${ARTIFACT_DIR}/git-warnings-${RUN_ID}.log"
MANIFEST_FILE="${ARTIFACT_DIR}/manifest-${RUN_ID}.env"
LATEST_MANIFEST_FILE="${ARTIFACT_DIR}/manifest.env"
RUN_TMP_DIR="$(mktemp -d "${TMP_BASE}/verify-clean-tmp.XXXXXX")"
WT_DIR="$(mktemp -d "${TMP_BASE}/clean-main.XXXXXX")"
RUNNER="$(id -un 2>/dev/null || echo unknown)@$(hostname 2>/dev/null || echo unknown)"

# --- worktree cleanup（EXIT INT TERM 兜底）----------------------------
cleanup_worktree() {
  if [ -d "$WT_DIR" ]; then
    git worktree remove --force "$WT_DIR" 2>/dev/null || rm -rf "$WT_DIR"
  fi
}
cleanup_all() {
  rm -rf "$RUN_TMP_DIR"
  cleanup_worktree
}
trap cleanup_all EXIT
trap 'cleanup_all; exit 99' INT TERM

# --- 環境前置 --------------------------------------------------------
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[FATAL] not inside a git work tree ($ROOT)" >&2
  exit 99
fi
resolve_origin_main() {
  git rev-parse origin/main^{commit} 2>/dev/null && return 0
  git fetch origin +refs/heads/main:refs/remotes/origin/main >/dev/null 2>>"$WARN_FILE" || return 1
  git rev-parse origin/main^{commit} 2>/dev/null
}

if ! OM_HASH="$(resolve_origin_main)"; then
  echo "[FATAL] origin/main 沒有可解析的 commit（先 git fetch origin main）" >&2
  exit 99
fi
HEAD_HASH_LANE="$(git rev-parse HEAD)"
LANE_AHEAD="$(git rev-list --count origin/main..HEAD)"

write_manifest() {
  local exit_code="$1"
  local manifest_tmp="${MANIFEST_FILE}.$$"
  {
    printf 'artifact_dir=%s\n' "$ARTIFACT_DIR"
    printf 'manifest_file=%s\n' "$MANIFEST_FILE"
    printf 'latest_manifest_file=%s\n' "$LATEST_MANIFEST_FILE"
    printf 'out_file=%s\n' "$OUT_FILE"
    printf 'warn_file=%s\n' "$WARN_FILE"
    printf 'run_id=%s\n' "$RUN_ID"
    printf 'run_time_utc=%s\n' "$TS"
    printf 'lane_branch=%s\n' "$LANE_BRANCH"
    printf 'lane_head=%s\n' "$HEAD_HASH_LANE"
    printf 'origin_main=%s\n' "$OM_HASH"
    printf 'runner=%s\n' "$RUNNER"
    printf 'exit_code=%s\n' "$exit_code"
  } > "$manifest_tmp" \
    && mv -f "$manifest_tmp" "$MANIFEST_FILE" \
    && cp "$MANIFEST_FILE" "${LATEST_MANIFEST_FILE}.$$" \
    && mv -f "${LATEST_MANIFEST_FILE}.$$" "$LATEST_MANIFEST_FILE"
}

write_manifest "running"

# --- 失敗路徑下也寫出 close-out 標頭（給讀者事實而非空字串）-----------
write_failure_evidence() {
  local reason="$1"
  {
    echo "# verify-clean.sh 結構化輸出（架構師定案 v2, 失敗路徑）"
    echo "# lane branch       : $LANE_BRANCH"
    echo "# lane HEAD         : $HEAD_HASH_LANE"
    echo "# origin/main       : $OM_HASH"
    echo "# run time (UTC)    : $TS"
    echo "# runner            : $RUNNER"
    echo "# 失敗原因          : $reason"
  } > "$OUT_FILE"
}

# --- 主流程（stdout → 主證據, stderr → warning log）------------------
{
  # === 標頭 ===
  echo "# verify-clean.sh 結構化輸出（架構師定案 v2 整合版）"
  echo "# lane branch       : $LANE_BRANCH"
  echo "# lane HEAD         : $HEAD_HASH_LANE"
  echo "# origin/main (前)  : $OM_HASH"
  echo "# lane ahead/behind : +$LANE_AHEAD -0  (task-1/#2 累積, 非 #3 scope)"
  echo "# branch (worktree) : HEAD (detached at origin/main) [期望]"
  echo "# 驗證對象 1        : published origin/main (via detached worktree) — release gate 視角"
  echo "# 驗證對象 2        : sandbox lane HEAD (直接跑) — 審計視角"
  echo "# worktree 路徑     : $WT_DIR"
  echo "# artefact manifest : $MANIFEST_FILE"
  echo "# 輸出證據檔        : $OUT_FILE"
  echo "# stderr warning 檔 : $WARN_FILE"
  echo "# run time (UTC)    : $TS"
  echo "# runner            : $RUNNER"
  echo

  fail=0

  # === Step 0a: lane 端 fetch（失敗顯眼標示,影響整體 exit）=========
  LANE_FETCH_TMP="$RUN_TMP_DIR/lane_fetch.out"
  LANE_FETCH_ERR="$RUN_TMP_DIR/lane_fetch.err"
  git fetch origin > "$LANE_FETCH_TMP" 2> "$LANE_FETCH_ERR"
  LANE_FETCH_RC=$?
  echo "## Step 0a: git fetch origin (lane 端) — fetch 失敗影響整體 exit ##"
  cat "$LANE_FETCH_TMP"
  if [ -s "$LANE_FETCH_ERR" ]; then
    cat "$LANE_FETCH_ERR" >> "$WARN_FILE"
    echo "(fetch stderr 進 warning log)"
  fi
  echo "exit: $LANE_FETCH_RC"
  FETCH_OK=1
  if [ "$LANE_FETCH_RC" -ne 0 ]; then
    FETCH_OK=0
    fail=1
    echo "[!] FETCH 失敗 — close-out 標頭顯眼標示: 「fetch 失敗, 比對結果作廢」"
  else
    echo "(fetch 成功)"
  fi
  echo

  # 重新抓 origin/main（fetch 成功後值可能更新）
  OM_HASH_POST="$(git rev-parse origin/main^{commit} 2>/dev/null || echo "$OM_HASH")"
  if [ "$OM_HASH" != "$OM_HASH_POST" ]; then
    echo "[!] fetch 期間 origin/main 更新: 前=$OM_HASH / 後=$OM_HASH_POST"
    echo "    worktree 仍綁前值 $OM_HASH;後續比對以此為準"
  fi
  echo

  # === Step 0b: 建 worktree ========================================
  echo "## Step 0b: git worktree add --detach (綁定 origin/main $OM_HASH_POST) ##"
  if [ -d "$WT_DIR" ]; then
    git worktree remove --force "$WT_DIR" 2>/dev/null || rm -rf "$WT_DIR"
  fi
  if ! git worktree add --detach "$WT_DIR" origin/main 2>>"$WARN_FILE"; then
    RC=$?
    echo "exit: $RC"
    echo "[!] worktree add 失敗, 程式中止"
    write_failure_evidence "worktree add 失敗 (exit=$RC)"
    fail=1
    exit 1
  fi
  echo "exit: 0"
  WT_HEAD="$(cd "$WT_DIR" && git rev-parse HEAD 2>>"$WARN_FILE")"
  echo "worktree 實測 HEAD: $WT_HEAD"
  if [ "$WT_HEAD" != "$OM_HASH_POST" ]; then
    write_failure_evidence "worktree HEAD ($WT_HEAD) != origin/main ($OM_HASH_POST), 綁定失敗"
    fail=1
    exit 1
  fi
  echo "(worktree HEAD == origin/main ✓)"
  echo

  # === Step 1: 結構性事實 ==========================================
  echo "## Step 1: 結構性事實（在 $WT_DIR 內）##"
  echo

  # --- 1.1 .gitmodules 實讀 + 解析區塊數（v2 第 7 條修正）---------
  echo "## 1.1 .gitmodules 實讀（[submodule] 區塊數偵測）##"
  LS_TMP="$RUN_TMP_DIR/ls-gitmodules.out"
  LS_ERR="$RUN_TMP_DIR/ls-gitmodules.err"
  (cd "$WT_DIR" && ls -la .gitmodules) > "$LS_TMP" 2> "$LS_ERR"
  echo '$ ls -la .gitmodules (worktree 內)'
  cat "$LS_TMP"
  if [ -s "$LS_ERR" ]; then
    cat "$LS_ERR" >> "$WARN_FILE"
    echo "(ls stderr 進 warning log)"
  fi
  echo

  GM_TMP="$RUN_TMP_DIR/gitmodules.out"
  GM_ERR="$RUN_TMP_DIR/gitmodules.err"
  (cd "$WT_DIR" && cat .gitmodules) > "$GM_TMP" 2> "$GM_ERR"
  GM_RC=$?
  echo '$ cat .gitmodules (worktree 內, 實讀內容)'
  cat "$GM_TMP"
  if [ -s "$GM_ERR" ]; then
    cat "$GM_ERR" >> "$WARN_FILE"
    echo "(cat stderr 進 warning log)"
  fi
  echo "cat exit: $GM_RC"
  echo

  # 解析 [submodule "..."] 區塊數
  if [ "$GM_RC" -ne 0 ] || [ ! -s "$GM_TMP" ]; then
    # 讀失敗 / 空檔 / Permission denied / 字元裝置 → 非常規檔
    echo "[判定] .gitmodules 為非常規檔 / 不可讀 / 空 → 視為「無 submodule 設定」"
    SUBMODULE_COUNT=0
  else
    SUBMODULE_COUNT="$(grep -c '^\[submodule ' "$GM_TMP" 2>/dev/null || echo 0)"
    echo "[判定] .gitmodules 解析成功, [submodule] 區塊數 = $SUBMODULE_COUNT"
    if [ "$SUBMODULE_COUNT" = "0" ]; then
      echo "       本 repo 無任何 submodule 設定 → submodule dirty 假性 diff 來源不適用"
    else
      echo "       本 repo 有 $SUBMODULE_COUNT 個 submodule 設定 → 後續驗證需特別注意"
    fi
  fi
  echo

  # --- 1.2 .gitattributes / core.autocrlf -------------------------
  echo "## 1.2 .gitattributes / core.autocrlf ##"
  (cd "$WT_DIR" && [ -f .gitattributes ] && echo ".gitattributes : present" || echo ".gitattributes : absent")
  AUTOCRLF_RAW="$(cd "$WT_DIR" && git config --get core.autocrlf 2>/dev/null || echo unset)"
  echo "core.autocrlf  : $AUTOCRLF_RAW"
  if [ "$AUTOCRLF_RAW" = "unset" ] || [ -z "$AUTOCRLF_RAW" ]; then
    echo "[判定] core.autocrlf 未顯式設定 → 使用 git 預設 (warning 視環境)"
  else
    echo "[判定] core.autocrlf = $AUTOCRLF_RAW"
  fi
  echo

  # --- 1.3 git submodule status（補充證據,非決定性）---------------
  echo "## 1.3 git submodule status（補充證據）##"
  SUB_TMP="$RUN_TMP_DIR/submodule.out"
  SUB_ERR="$RUN_TMP_DIR/submodule.err"
  (cd "$WT_DIR" && git submodule status) > "$SUB_TMP" 2> "$SUB_ERR"
  echo '$ git submodule status (worktree 內)'
  cat "$SUB_TMP"
  if [ -s "$SUB_ERR" ]; then cat "$SUB_ERR" >> "$WARN_FILE"; fi
  echo "exit: $?"
  echo

  # === Step 2-5: 4 條驗證命令（worktree 內 = published origin/main 視角）===
  echo "## Step 2-5: 4 條驗證命令（worktree 內 = published origin/main 視角）##"
  echo "(以下為 worktree 內命令; --quiet 靜默契約: 0=無, 1=有)"
  echo

  # 2) status
  STATUS_TMP="$RUN_TMP_DIR/status.out"
  STATUS_ERR="$RUN_TMP_DIR/status.err"
  (cd "$WT_DIR" && git status --porcelain=v2 --branch --untracked-files=normal) > "$STATUS_TMP" 2> "$STATUS_ERR"
  STATUS_RC=$?
  echo "## 2) git status --porcelain=v2 --branch --untracked-files=normal ##"
  cat "$STATUS_TMP"
  if [ -s "$STATUS_ERR" ]; then cat "$STATUS_ERR" >> "$WARN_FILE"; fi
  echo "exit: $STATUS_RC"
  [ "$STATUS_RC" -eq 0 ] || fail=1
  echo

  # 3) diff origin/main HEAD
  echo "## 3) git diff --quiet origin/main HEAD ##"
  echo "(worktree HEAD==origin/main 同一 commit → 必為 0)"
  (cd "$WT_DIR" && git diff --quiet origin/main HEAD) 2>>"$WARN_FILE"
  DIFF_RC=$?
  echo "exit: $DIFF_RC"
  [ "$DIFF_RC" -eq 0 ] || fail=1
  echo

  # 4) diff --cached
  echo "## 4) git diff --quiet --cached ##"
  echo "(worktree 新建 → 必為 0)"
  (cd "$WT_DIR" && git diff --quiet --cached) 2>>"$WARN_FILE"
  CACHED_RC=$?
  echo "exit: $CACHED_RC"
  [ "$CACHED_RC" -eq 0 ] || fail=1
  echo

  # 5) hash 比對
  LHS="$(cd "$WT_DIR" && git rev-parse HEAD 2>>"$WARN_FILE")"
  RHS="$(cd "$WT_DIR" && git rev-parse origin/main 2>>"$WARN_FILE")"
  HASH_RC=0
  echo "## 5) rev-parse HEAD vs origin/main ##"
  echo "HEAD        = $LHS"
  echo "origin/main = $RHS"
  if [ "$LHS" = "$RHS" ]; then
    echo "result      = MATCH"
  else
    echo "result      = DIFFER"
    HASH_RC=1
  fi
  echo "exit: $HASH_RC"
  [ "$HASH_RC" -eq 0 ] || fail=1
  echo

  # === Step 6: 滿足狀況盤點（給讀者的事實,不下結論）=================
  echo "## Step 6: 4 條命令滿足狀況盤點 ##"
  FILE_LINES="$(grep -v '^# ' "$STATUS_TMP" 2>/dev/null || true)"
  if [ -z "$FILE_LINES" ]; then
    echo "  [1] status 無檔案行 (工作樹乾淨) : 滿足"
  else
    echo "  [1] status 無檔案行 (工作樹乾淨) : 不滿足"
    printf '%s\n' "$FILE_LINES" | sed 's/^/      /'
  fi
  if [ "$DIFF_RC" -eq 0 ]; then
    echo "  [2] diff --quiet origin/main HEAD : 滿足 (無 diff)"
  else
    echo "  [2] diff --quiet origin/main HEAD : 不滿足"
  fi
  if [ "$CACHED_RC" -eq 0 ]; then
    echo "  [3] diff --quiet --cached          : 滿足 (無 staged)"
  else
    echo "  [3] diff --quiet --cached          : 不滿足"
  fi
  if [ "$HASH_RC" -eq 0 ]; then
    echo "  [4] HEAD hash == origin/main hash   : 滿足"
  else
    echo "  [4] HEAD hash == origin/main hash   : 不滿足"
  fi
  echo

  # === Step 7: 與驗收標準對齊點 ====================================
  echo "## Step 7: 與驗收標準對齊點 ##"
  echo "  驗收條款 'branch.ab +0 -0'     : worktree HEAD==origin/main, status 應出 '# branch.ab +0 -0'"
  echo "  驗收條款 'diff --quiet exit 0' : worktree HEAD 與 origin/main 同一 commit, diff 必為空"
  echo "  驗收條款 'hash 一致'           : 同上理由必成立"
  echo "  驗收條款 '工作樹乾淨'          : worktree 新建、未改動、應無 untracked / modified"
  echo

  # === Step 8: lane 端實況（sandbox HEAD 視角,誠實記錄）=============
  echo "## Step 8: lane 端實況（sandbox HEAD 視角, 非 worktree, 誠實記錄）##"
  echo "(以下命令在 lane 端直接跑、不用 worktree, 反映 sandbox HEAD 對 origin/main 的真實落差)"
  echo "(已知 lane HEAD 領先 origin/main $LANE_AHEAD commits = task-1/#2 合法累積, 非 #3 scope)"
  echo

  LANE_STATUS_TMP="$RUN_TMP_DIR/lane_status.out"
  LANE_STATUS_ERR="$RUN_TMP_DIR/lane_status.err"
  git status --porcelain=v2 --branch --untracked-files=normal > "$LANE_STATUS_TMP" 2> "$LANE_STATUS_ERR"
  echo '$ git status --porcelain=v2 --branch --untracked-files=normal (lane 端)'
  cat "$LANE_STATUS_TMP"
  if [ -s "$LANE_STATUS_ERR" ]; then cat "$LANE_STATUS_ERR" >> "$WARN_FILE"; fi
  echo "exit: $?"
  echo

  echo '$ git diff --quiet origin/main HEAD (lane 端, 預期 exit 1 = 領先 $LANE_AHEAD commits)'
  git diff --quiet origin/main HEAD 2>>"$WARN_FILE"
  LANE_DIFF_RC=$?
  echo "exit: $LANE_DIFF_RC"
  echo

  LANE_CACHED_TMP="$RUN_TMP_DIR/lane_cached.out"
  LANE_CACHED_ERR="$RUN_TMP_DIR/lane_cached.err"
  git diff --cached > "$LANE_CACHED_TMP" 2> "$LANE_CACHED_ERR"
  echo '$ git diff --cached (lane 端, 有差時才有輸出)'
  if [ -s "$LANE_CACHED_TMP" ]; then cat "$LANE_CACHED_TMP"; else echo "(空: 無 staged)"; fi
  if [ -s "$LANE_CACHED_ERR" ]; then cat "$LANE_CACHED_ERR" >> "$WARN_FILE"; fi
  echo "exit: $?"
  echo

  echo '$ git rev-parse HEAD vs origin/main (lane 端)'
  echo "HEAD        = $(git rev-parse HEAD)"
  echo "origin/main = $(git rev-parse origin/main)"
  echo "(lane 端 hash MISMATCH 預期內: 差距 $LANE_AHEAD commits = task-1/#2 累積)"
  echo

  # === Step 9: 結論分組（架構定案第 4 條）==========================
  echo "## Step 9: 結論分組（供 close-out 文件直接引用）##"
  echo
  echo "### 會通過新標尺（任務 #3 對工作樹/版控零新增）"
  echo "  - worktree 內 status 無檔案行"
  echo "  - worktree 內 diff --cached exit 0"
  echo "  - worktree 內 hash MATCH"
  echo "  - lane 端 status 無檔案行（若 lane 無殘留）"
  echo "  - lane 端 diff --cached exit 0（若 lane 無 staged）"
  echo
  echo "### 不會通過舊標尺（HEAD == origin/main, 任務 lane 不可滿足）"
  echo "  - lane 端 diff --quiet origin/main HEAD exit 1（差距 $LANE_AHEAD commits = task-1/#2 累積）"
  echo "  - lane 端 hash 比對 MISMATCH（同上原因）"
  echo "  - lane 端無 branch.ab 段（task-3 無 upstream）"
  echo
  echo "### 已知沙箱產物（不影響判定, exit code 仍正確）"
  echo "  - .gitmodules 非常規檔 / 不可讀 → 視為「無 submodule 設定」（Step 1.1 判定）"
  echo "  - git fetch 在沙箱可能 warn「not a git repository」之類雜訊, exit 0 不受影響"
  echo "  - git submodule status 可能列出 orphan gitlink（與 .gitmodules 內容無衝突, 差異源自 lane 端字元裝置）"
  echo
  if [ "$FETCH_OK" -eq 0 ]; then
    echo "### [!] fetch 失敗（此輪若發生）"
    echo "  - close-out 標頭顯眼標示「fetch 失敗, 比對結果作廢」"
    echo "  - 整體 exit = 1（不論 4 條命令本身是否成功）"
  fi
  echo

  echo "=== 程式 fail=$fail（反映 fetch + 4 條命令 + worktree 綁定, 非驗收結論） ==="
} > "$OUT_FILE" 2> "$WARN_FILE"

OVERALL_RC="${fail:-$?}"
write_manifest "$OVERALL_RC"
if [ -f "$OUT_FILE" ]; then
  cat "$OUT_FILE"
fi
exit "$OVERALL_RC"
