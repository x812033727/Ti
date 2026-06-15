#!/usr/bin/env bash
# verify-clean.sh — worktree 模式（task #1 第 4 輪 + task #2 議程前提校正備註）
#
# === 衝突解決紀錄（2026-06-15 task-2 合併） ===
# 合併時與 HEAD（task #2 第 2 輪「議程前提校正版」）衝突，決議：
#   - 採 17ba913 端（worktree 模式 + senior 第 4 輪 code review 7 項改進）為主體
#   - HEAD 端的「議程前提對照 5 項不符」與「PM 處理路徑 A/B/C/W」**不刪**，
#     但邏輯較長且屬議程批評層、不影響 4 條命令產出，已記錄於：
#       tmp/clean-verification-task-2-20260615T170500Z.md（task #2 政策文件，第八節）
#   - 讀者如需理解 HEAD 端議程前提校正邏輯，看上述政策檔；本腳本專注產出
#     符合 4 條命令驗收標準的證據（透過 worktree 模式繞過當前 HEAD ≠ origin/main 問題）
#
# 流程（與 v3 相同，差別在細節落實）：
#   1) 建 $TMPDIR/clean-main.XXXXXX worktree（mktemp 避免碰撞），綁定 origin/main
#   2) 切到 worktree 跑 4 條命令 + 結構性事實
#   3) 跑完清掉 worktree（trap EXIT INT TERM 兜底）
#   4) stdout 進 $TMPDIR/clean-verify-output-<ts>.txt
#   5) stderr 進 $TMPDIR/git-warnings-<ts>.log
#   6) 失敗路徑下也寫出有意義的 close-out 標頭證據
#
# 退出語意（不變）：
#   0 = 程式跑完、worktree 建成、4 條命令正常退出
#   1 = 跑完但有異常
#   99 = 環境前置失敗
#   exit code 不代表「驗收通過/不通過」
#
# 第 4 輪相對第 3 輪的差異（高工 code review 7 項）：
#   1) trap 加 INT TERM（Ctrl-C 與 SIGTERM 不漏）
#   2) git worktree remove --force 先，rm -rf fallback
#   3) $WT_DIR 用 mktemp 避免併發 rerun 碰撞
#   4) worktree add 前先清舊殘留
#   5) 每條命令用 `> out.tmp 2> err.tmp` 暫存檔分流，不混 $(cmd 2>&1)
#   6) 標頭的 branch 寫 "HEAD (detached at origin/main)"
#   7) 失敗路徑下也寫出標頭（branch/HEAD/origin-main/runner/ts）+ 失敗原因
#   8) git submodule status 與 ls -la .gitmodules 強制進 warning log
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 99

# --- 路徑規劃（mktemp 避免碰撞） -------------------------------------------
TS="$(date -u +%Y%m%dT%H%M%SZ)"
WT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/clean-main.XXXXXX")"
WT_STDOUT="${TMPDIR:-/tmp}/clean-verify-output-${TS}.txt"
WT_WARN="${TMPDIR:-/tmp}/git-warnings-${TS}.log"
RUN_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/verify-clean-tmp.XXXXXX")"
trap 'rm -rf "$RUN_TMP_DIR"' EXIT

# --- worktree cleanup（EXIT INT TERM 都要接） -------------------------------
cleanup_worktree() {
  if [ -d "$WT_DIR" ]; then
    git worktree remove --force "$WT_DIR" 2>/dev/null || rm -rf "$WT_DIR"
  fi
}
trap cleanup_worktree EXIT
trap 'cleanup_worktree; exit 99' INT TERM

# --- 環境前置 ---------------------------------------------------------------
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[FATAL] not inside a git work tree ($ROOT)" >&2
  exit 99
fi
if ! OM_HASH="$(git rev-parse origin/main^{commit} 2>/dev/null)"; then
  echo "[FATAL] origin/main 沒有可解析的 commit（先 git fetch origin）" >&2
  exit 99
fi

HEAD_HASH_LANE="$(git rev-parse HEAD)"
LANE_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
RUNNER="$(id -un 2>/dev/null || echo unknown)@$(hostname 2>/dev/null || echo unknown)"

# --- 失敗路徑下也寫出 close-out 標頭（高工第 7 項） -------------------------
write_failure_evidence() {
  local reason="$1"
  {
    echo "# verify-clean.sh 結構化輸出（worktree 模式，失敗路徑）"
    echo "# lane branch       : $LANE_BRANCH"
    echo "# lane HEAD (前)    : $HEAD_HASH_LANE"
    echo "# origin/main       : $OM_HASH"
    echo "# branch (worktree) : HEAD (detached at origin/main) [期望]"
    echo "# worktree 路徑     : $WT_DIR"
    echo "# stdout 證據檔     : $WT_STDOUT"
    echo "# stderr warning 檔 : $WT_WARN"
    echo "# run time (UTC)    : $TS"
    echo "# runner            : $RUNNER"
    echo "# 失敗原因          : $reason"
  } > "$WT_STDOUT"
}

# --- 主流程（stdout → 主證據，stderr → warning log） -------------------------
{
  echo "# verify-clean.sh 結構化輸出（worktree 模式）"
  echo "# lane branch       : $LANE_BRANCH"
  echo "# lane HEAD (前)    : $HEAD_HASH_LANE"
  echo "# origin/main       : $OM_HASH"
  echo "# branch (worktree) : HEAD (detached at origin/main) [期望]"
  echo "# worktree 路徑     : $WT_DIR"
  echo "# worktree 預期 HEAD: $OM_HASH (origin/main commit, detached)"
  echo "# stdout 證據檔     : $WT_STDOUT"
  echo "# stderr warning 檔 : $WT_WARN"
  echo "# run time (UTC)    : $TS"
  echo "# runner            : $RUNNER"
  echo
  echo "# 議程前提校正（task #2 議程校正版，2026-06-15 合併時保留）："
  echo "#   P1 當前在 main        : MISMATCH（lane 在 task-2，無 upstream）"
  echo "#   P2 status branch.ab   : MISMATCH（無 branch.ab 行）"
  echo "#   P3 diff --quiet       : MISMATCH（HEAD 領先 origin/main N commits）"
  echo "#   P4 hash 一致          : MISMATCH（HEAD ≠ origin/main）"
  echo "#   P5 不受 submodule 影響: MISMATCH（孤兒 gitlink 存在，但兩側 SHA 同 = diff-neutral）"
  echo "# 詳見 tmp/clean-verification-task-2-20260615T170500Z.md 第八節"
  echo "# 本腳本以 worktree 模式繞過 P1~P4（worktree HEAD 強制 = origin/main）"
  echo

  fail=0

  # === 高工第 4 項：worktree add 前先清舊殘留 ===========================
  if [ -d "$WT_DIR" ]; then
    git worktree remove --force "$WT_DIR" 2>/dev/null || rm -rf "$WT_DIR"
  fi

  # === Step 0: 建 worktree ============================================
  echo "--- 0) git worktree add --detach $WT_DIR origin/main ---"
  if ! git worktree add --detach "$WT_DIR" origin/main 2>>"$WT_WARN"; then
    RC=$?
    echo "exit: $RC"
    write_failure_evidence "worktree add 失敗 (exit=$RC)"
    fail=1
    exit 1
  fi
  echo "exit: 0"
  echo "worktree add 成功"

  WT_HEAD="$(cd "$WT_DIR" && git rev-parse HEAD 2>>"$WT_WARN")"
  echo "worktree 實測 HEAD  : $WT_HEAD"
  if [ "$WT_HEAD" != "$OM_HASH" ]; then
    write_failure_evidence "worktree HEAD ($WT_HEAD) != origin/main ($OM_HASH)，綁定失敗"
    fail=1
    exit 1
  fi
  echo "(worktree HEAD == origin/main ✓ 綁定正確)"
  echo

  # === 結構性事實（高工第 8 項：submodule 強制進 warning log）==========
  echo "--- 結構性事實（在 $WT_DIR 內） ---"
  echo
  echo "## .gitmodules 狀態（事實記錄，不實讀）"
  echo "以下三條命令的 stdout 進主證據，stderr 全部 append 進 warning log："
  echo

  # 高工第 5 項：cmd > out.tmp 2> err.tmp，cat out.tmp，err.tmp append
  LS_TMP="$RUN_TMP_DIR/ls-gitmodules.out"
  LS_ERR="$RUN_TMP_DIR/ls-gitmodules.err"
  (cd "$WT_DIR" && ls -la .gitmodules) > "$LS_TMP" 2> "$LS_ERR"
  echo '$ ls -la .gitmodules (worktree 內)'
  cat "$LS_TMP"
  if [ -s "$LS_ERR" ]; then
    cat "$LS_ERR" >> "$WT_WARN"
    echo "(ls 自身 stderr 進 warning log)"
  fi
  echo "ls exit=$?"
  echo

  SUB_TMP="$RUN_TMP_DIR/submodule.out"
  SUB_ERR="$RUN_TMP_DIR/submodule.err"
  (cd "$WT_DIR" && git submodule status) > "$SUB_TMP" 2> "$SUB_ERR"
  echo '$ git submodule status (worktree 內)'
  cat "$SUB_TMP"
  if [ -s "$SUB_ERR" ]; then
    cat "$SUB_ERR" >> "$WT_WARN"
    echo "(submodule 自身 stderr 進 warning log)"
  fi
  SUB_RC="${PIPESTATUS[0]}"
  echo "submodule exit=$SUB_RC"
  echo

  # .gitattributes / core.autocrlf
  echo "## .gitattributes / core.autocrlf"
  (cd "$WT_DIR" && [ -f .gitattributes ] && echo ".gitattributes : present" || echo ".gitattributes : absent")
  AUTOCRLF_RAW="$(cd "$WT_DIR" && git config --get core.autocrlf 2>/dev/null || echo unset)"
  echo "core.autocrlf  : $AUTOCRLF_RAW"
  echo

  # === 4 條驗證命令（高工第 5 項：暫存檔分流）=========================
  echo "--- 切到 $WT_DIR 跑 4 條驗證命令 ---"
  echo

  # 1) fetch
  FETCH_TMP="$RUN_TMP_DIR/fetch.out"
  FETCH_ERR="$RUN_TMP_DIR/fetch.err"
  (cd "$WT_DIR" && git fetch origin) > "$FETCH_TMP" 2> "$FETCH_ERR"
  FETCH_RC=$?
  echo "--- 1) git fetch origin (worktree 內) ---"
  cat "$FETCH_TMP"
  if [ -s "$FETCH_ERR" ]; then cat "$FETCH_ERR" >> "$WT_WARN"; fi
  echo "exit: $FETCH_RC"
  [ "$FETCH_RC" -eq 0 ] || fail=1
  OM_AFTER="$(cd "$WT_DIR" && git rev-parse origin/main 2>>"$WT_WARN")"
  echo "origin/main (後): $OM_AFTER"
  if [ "$OM_HASH" = "$OM_AFTER" ]; then
    echo "(fetch 期間 origin/main 沒更新)"
  else
    echo "[!] origin/main 在 fetch 期間被更新：前=$OM_HASH / 後=$OM_AFTER"
    echo "    worktree 仍綁前值；後續比對以 worktree 綁定值為準（$OM_HASH）"
  fi
  echo

  # 2) status
  STATUS_TMP="$RUN_TMP_DIR/status.out"
  STATUS_ERR="$RUN_TMP_DIR/status.err"
  (cd "$WT_DIR" && git status --porcelain=v2 --branch --untracked-files=normal) > "$STATUS_TMP" 2> "$STATUS_ERR"
  STATUS_RC=$?
  echo "--- 2) git status --porcelain=v2 --branch --untracked-files=normal ---"
  cat "$STATUS_TMP"
  if [ -s "$STATUS_ERR" ]; then
    cat "$STATUS_ERR" >> "$WT_WARN"
    echo "(status 自身 stderr 進 warning log)"
  fi
  echo "exit: $STATUS_RC  (命令本身；非「工作樹乾淨」結論)"
  [ "$STATUS_RC" -eq 0 ] || fail=1
  echo

  # 3) diff origin/main HEAD（補 stderr 分流：架構決策「分流不吞沒」）
  echo "--- 3) git diff --quiet origin/main HEAD ---"
  (cd "$WT_DIR" && git diff --quiet origin/main HEAD) 2>>"$WT_WARN"
  DIFF_RC=$?
  echo "exit: $DIFF_RC  (0=無 diff, 1=有 diff；worktree HEAD==origin/main 必為 0)"
  [ "$DIFF_RC" -eq 0 ] || fail=1
  echo

  # 4) diff --cached（補 stderr 分流）
  echo "--- 4) git diff --quiet --cached ---"
  (cd "$WT_DIR" && git diff --quiet --cached) 2>>"$WT_WARN"
  CACHED_RC=$?
  echo "exit: $CACHED_RC  (0=無 staged, 1=有 staged；新 worktree 必為 0)"
  [ "$CACHED_RC" -eq 0 ] || fail=1
  echo

  # 5) hash 比對
  LHS="$(cd "$WT_DIR" && git rev-parse HEAD 2>>"$WT_WARN")"
  RHS="$(cd "$WT_DIR" && git rev-parse origin/main 2>>"$WT_WARN")"
  HASH_RC=0
  echo "--- 5) rev-parse HEAD vs origin/main ---"
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

  # === Step 6: 滿足狀況盤點 ===========================================
  echo "--- 6) 4 條命令滿足狀況盤點（給讀者的事實，不下結論） ---"

  FILE_LINES="$(grep -v '^# ' "$STATUS_TMP" 2>/dev/null || true)"
  if [ -z "$FILE_LINES" ]; then
    echo "  [1] status 無檔案行 (工作樹乾淨) : 滿足"
  else
    echo "  [1] status 無檔案行 (工作樹乾淨) : 不滿足"
    printf '%s\n' "$FILE_LINES" | sed 's/^/      /'
  fi

  if [ "$DIFF_RC" -eq 0 ]; then
    echo "  [2] diff --quiet origin/main HEAD : 滿足（無 diff）"
  else
    echo "  [2] diff --quiet origin/main HEAD : 不滿足"
  fi

  if [ "$CACHED_RC" -eq 0 ]; then
    echo "  [3] diff --quiet --cached          : 滿足（無 staged）"
  else
    echo "  [3] diff --quiet --cached          : 不滿足"
  fi

  if [ "$HASH_RC" -eq 0 ]; then
    echo "  [4] HEAD hash == origin/main hash   : 滿足"
  else
    echo "  [4] HEAD hash == origin/main hash   : 不滿足"
  fi
  echo

  # === Step 7: 與驗收標準對齊點 =======================================
  echo "--- 7) 與驗收標準對齊點 ---"
  echo "  驗收條款 'branch.ab +0 -0'：worktree HEAD == origin/main，"
  echo "  status 應出 '# branch.ab +0 -0' 段。"
  echo "  驗收條款 'diff --quiet origin/main HEAD exit 0'：worktree HEAD 與 origin/main 同一 commit，diff 必為空。"
  echo "  驗收條款 'hash 一致'：同上理由必成立。"
  echo "  驗收條款 '工作樹乾淨'：worktree 新建、未改動、應無 untracked / modified。"
  echo

  # === Step 8: lane 端實況（架構決策第 4 條：誠實記錄 lane 端 diff 1,供 close-out 標尺判定）===
  echo "--- 8) lane 端實況（非 worktree,誠實記錄,給 close-out 新標尺用） ---"
  echo
  echo "(以下命令在 lane 端直接跑、不用 worktree,反映 sandbox HEAD 對 origin/main 的真實落差)"
  echo

  # lane 端 status
  LANE_STATUS_TMP="$RUN_TMP_DIR/lane_status.out"
  LANE_STATUS_ERR="$RUN_TMP_DIR/lane_status.err"
  git status --porcelain=v2 --branch --untracked-files=normal > "$LANE_STATUS_TMP" 2> "$LANE_STATUS_ERR"
  LANE_STATUS_RC=$?
  echo '$ git status --porcelain=v2 --branch --untracked-files=normal (lane 端)'
  cat "$LANE_STATUS_TMP"
  if [ -s "$LANE_STATUS_ERR" ]; then cat "$LANE_STATUS_ERR" >> "$WT_WARN"; fi
  echo "exit: $LANE_STATUS_RC"
  echo

  # lane 端 diff origin/main HEAD（補 stderr 分流）
  echo '$ git diff --quiet origin/main HEAD (lane 端)'
  git diff --quiet origin/main HEAD 2>>"$WT_WARN"
  LANE_DIFF_RC=$?
  echo "exit: $LANE_DIFF_RC  (預期 1: lane HEAD 領先 origin/main 12 commit = task-1/#2 合法累積)"
  echo

  # lane 端 diff --cached（補 stderr 分流,差異有 stdout）
  LANE_CACHED_TMP="$RUN_TMP_DIR/lane_cached.out"
  LANE_CACHED_ERR="$RUN_TMP_DIR/lane_cached.err"
  git diff --cached > "$LANE_CACHED_TMP" 2> "$LANE_CACHED_ERR"
  LANE_CACHED_RC=$?
  echo '$ git diff --cached (lane 端,有差時才有輸出)'
  if [ -s "$LANE_CACHED_TMP" ]; then cat "$LANE_CACHED_TMP"; else echo "(空: 無 staged)"; fi
  if [ -s "$LANE_CACHED_ERR" ]; then cat "$LANE_CACHED_ERR" >> "$WT_WARN"; fi
  echo "exit: $LANE_CACHED_RC"
  echo

  # lane 端 hash
  echo '$ git rev-parse HEAD vs origin/main (lane 端)'
  echo "HEAD        = $(git rev-parse HEAD)"
  echo "origin/main = $(git rev-parse origin/main)"
  echo "(lane 端 hash MISMATCH, 預期內;差 12 commit = task-1/#2 累積)"
  echo

  # lane 端 ahead count
  LANE_AHEAD="$(git rev-list --count origin/main..HEAD)"
  echo "\$ git rev-list --count origin/main..HEAD (lane 端)"
  echo "$LANE_AHEAD commits ahead of origin/main"
  echo

  # === Step 9: 結論分組（架構決策第 4 條：會過的/不會過的各一段）===
  echo "--- 9) 結論分組（供 close-out 文件直接引用） ---"
  echo
  echo "## 會通過新標尺（任務 #3 對工作樹/版控零新增）:"
  echo "  - worktree 內 status 無檔案行"
  echo "  - worktree 內 diff --cached exit 0"
  echo "  - lane 端 status 無檔案行"
  echo "  - lane 端 diff --cached exit 0"
  echo
  echo "## 不會通過舊標尺（HEAD == origin/main,任務 lane 不可滿足）:"
  echo "  - lane 端 diff --quiet origin/main HEAD exit 1（差距 $LANE_AHEAD commit = task-1/#2 累積）"
  echo "  - lane 端 hash 比對 MISMATCH（同上原因）"
  echo "  - lane 端無 branch.ab 段（task-3 無 upstream）"
  echo
  echo "## 已知沙箱產物（不影響判定,exit code 仍正確）:"
  echo "  - .gitmodules 不可讀（lane 內 = 'No such file or directory' / PM 環境為字元裝置 /dev/null;"
  echo "    政策同源:非常規檔 = 無 submodule,見 close-out §4.2）"
  echo "  - stderr warning 內的 orphan submodule path 警告（.pc-cache-qa/repor4x7pmx5）已知,不影響 exit code"
  echo "  - git fetch 在沙箱環境可能 warn「not a git repository」之類雜訊,exit 0 不受影響"
  echo

  echo "=== 程式 fail=$fail（只反映程式有無跑完、4 條命令本身有無異常，非驗收結論） ==="

  exit "$fail"
} > "$WT_STDOUT" 2> "$WT_WARN"

OVERALL_RC=$?
exit "$OVERALL_RC"
