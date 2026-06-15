#!/usr/bin/env bash
# scripts/verify-clean.sh
# 任務 #2 驗證腳本：證明當前工作樹與 origin/main 處於空 diff 狀態（或誠實報告非空）。
# 退出 0 語意 = 全部 4 條驗證命令「如預期」、fetch 成功、且假性 diff 偵測無誤判可能。
# 退出 1 語意 = 4 條命令任一非預期 OR fetch 失敗 OR 假性 diff 偵測發現潛在 mask 風險。
#
# 設計依據：architect 整合 senior/engineer 兩輪意見後的決策清單（DECISIONS.md / 會議紀錄）。
# 輸出格式：每條命令「$ <cmd> / <stdout+stderr> / exit: N」三行一組；末尾附「總體結論」與
# 「假性 diff 排除政策」兩段。輸出到 $TMPDIR/clean-verify-output-<branch>-<ts>.txt，
# 不入版控、不污染工作樹。
#
# 對應的 4 條驗證命令（前置防呆不計入）：
#   1. git status --porcelain=v2 --branch --untracked-files=normal
#   2. git diff --quiet origin/main HEAD
#   3. git diff --quiet --cached
#   4. [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || exit 99

# 環境變數：給輸出檔與 close-out 文件用的路徑前綴；外部覆寫可換到別的 $TMPDIR
TMPDIR="${TMPDIR:-/tmp}"

# 動態參數
_RAW_HEAD="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
if [ "$_RAW_HEAD" = "HEAD" ]; then
  BRANCH="DETACHED-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
else
  BRANCH="$_RAW_HEAD"
fi
BRANCH_SAFE="$(printf '%s' "$BRANCH" | tr '/\\' '__')"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$TMPDIR/clean-verify-output-${BRANCH_SAFE}-${TS}.txt"

# 聚合 fail 計數；任何 4 條命令非預期或 fetch 失敗都 +1
fail=0
fetch_failed=0
has_pseudo_diff_risk=0

# 重新導向整段輸出到 OUT 檔；同時 echo 到 stdout 供 live 觀察
exec > >(tee "$OUT") 2>&1

echo "============================================================"
echo "verify-clean.sh  執行報告"
echo "============================================================"
echo "branch:           $BRANCH"
echo "HEAD:             $(git rev-parse HEAD 2>/dev/null || echo 'N/A')"
echo "origin/main:      $(git rev-parse origin/main 2>/dev/null || echo 'N/A')"
echo "fetch timestamp:  $TS (UTC)"
echo "executor:         ${USER:-$(id -un 2>/dev/null || echo 'unknown')}"
echo "cwd:              $ROOT"
echo "output file:      $OUT"
echo "============================================================"
echo

# --- 階段 A：前置防呆（不計入 4 條，但需記 exit code）---
echo "## 階段 A：前置防呆"
echo

echo "\$ git rev-parse --is-inside-work-tree"
git rev-parse --is-inside-work-tree
echo "exit: $?"
ISREPO=$?
if [ "$ISREPO" != "0" ]; then
  echo "[FAIL] 不在 git 工作樹內，提早結束"; echo; fail=$((fail+1))
  echo "[總體] fail=$fail (前置失敗)"; exit $fail
fi
echo

echo "\$ git rev-parse --verify origin/main^{commit}"
git rev-parse --verify origin/main^{commit} 2>&1 || echo "(ref 不存在)"
echo "exit: $?"
ORGREV=$?
if [ "$ORGREV" != "0" ]; then
  echo "[FAIL] origin/main^{commit} 不存在，提早結束"; echo; fail=$((fail+1))
  echo "[總體] fail=$fail (前置失敗)"; exit $fail
fi
echo

# --- 階段 B：fetch（不啟用 set -e，記 exit code；失敗不早死）---
echo "## 階段 B：git fetch origin"
echo
echo "\$ git fetch origin"
git fetch origin 2>&1
echo "exit: $?"
FETCH_RC=$?
if [ "$FETCH_RC" != "0" ]; then
  echo "[FETCH FAILED] exit=$FETCH_RC；依決策，比對結果作廢"
  fetch_failed=1
  fail=$((fail+1))
fi
echo

# --- 階段 C：4 條驗證命令（全部跑完才結束）---
echo "## 階段 C：4 條驗證命令"
echo

# (1) status --porcelain=v2 --branch --untracked-files=normal
echo "\$ git status --porcelain=v2 --branch --untracked-files=normal"
ST_OUT="$(git status --porcelain=v2 --branch --untracked-files=normal 2>&1)"
ST_RC=$?
printf '%s\n' "$ST_OUT"
echo "exit: $ST_RC"
# 預期：exit 0 且無檔案行（除 # branch.* 開頭的 header 行外）
if [ "$ST_RC" = "0" ] && ! printf '%s\n' "$ST_OUT" | grep -vE '^# branch\.' | grep -q .; then
  echo "[OK] 工作樹乾淨（僅有 branch header，無檔案行）"
else
  echo "[FAIL] 工作樹非乾淨或 status 報錯"
  fail=$((fail+1))
fi
echo

# (2) diff --quiet origin/main HEAD
echo "\$ git diff --quiet origin/main HEAD"
DIFF_OUT="$(git diff --quiet origin/main HEAD 2>&1; echo "RC=$?")"
DIFF_RC="${DIFF_OUT##*RC=}"
echo "(stdout 為空；契約：exit 0 = 無 diff，exit 1 = 有 diff，輸出至 stderr)"
echo "exit: $DIFF_RC"
if [ "$DIFF_RC" = "0" ]; then
  echo "[OK] 與 origin/main 無 diff"
else
  echo "[FAIL] 與 origin/main 有 diff (exit=$DIFF_RC)"
  fail=$((fail+1))
fi
echo

# (3) diff --quiet --cached
echo "\$ git diff --quiet --cached"
CACH_OUT="$(git diff --quiet --cached 2>&1; echo "RC=$?")"
CACH_RC="${CACH_OUT##*RC=}"
echo "(stdout 為空；契約：exit 0 = 無 staged，exit 1 = 有 staged)"
echo "exit: $CACH_RC"
if [ "$CACH_RC" = "0" ]; then
  echo "[OK] 無 staged 變更"
else
  echo "[FAIL] 有 staged 變更 (exit=$CACH_RC)"
  fail=$((fail+1))
fi
echo

# (4) hash 比對
echo "\$ [ \"\$(git rev-parse HEAD)\" = \"\$(git rev-parse origin/main)\" ]"
H1="$(git rev-parse HEAD 2>/dev/null || echo NONE)"
H2="$(git rev-parse origin/main 2>/dev/null || echo NONE)"
echo "HEAD:        $H1"
echo "origin/main: $H2"
if [ "$H1" = "$H2" ] && [ "$H1" != "NONE" ]; then
  echo "[OK] HEAD hash 與 origin/main 完全一致"
  HASH_RC=0
else
  echo "[FAIL] HEAD hash 與 origin/main 不一致"
  fail=$((fail+1))
  HASH_RC=1
fi
echo "exit: $HASH_RC"
echo

# --- 階段 D：假性 diff 排除政策偵測（不修補，只盤點）---
echo "## 階段 D：假性 diff 排除政策偵測（不修補，只盤點）"
echo

# D-1: .gitmodules 內容（不存在 vs 存在 vs 空檔 vs 有 [submodule] 區塊）
echo "### D-1: .gitmodules 內容偵測"
if [ ! -e "$ROOT/.gitmodules" ]; then
  echo "狀態: 不存在"
  echo "結論: 本 repo 無 .gitmodules 設定檔，submodule 機制未啟用"
elif [ ! -s "$ROOT/.gitmodules" ]; then
  echo "狀態: 存在但為空檔"
  echo "結論: .gitmodules 空檔無有效 [submodule] 區塊，submodule 機制未啟用"
else
  echo "內容:"
  sed 's/^/    /' "$ROOT/.gitmodules"
  SUB_COUNT="$(grep -cE '^\[submodule' "$ROOT/.gitmodules" || true)"
  echo "結論: .gitmodules 內有 $SUB_COUNT 個 [submodule] 區塊"
  if [ "${SUB_COUNT:-0}" -gt 0 ]; then
    echo "[WARN] 本 repo 存在 submodule 設定；後續 status/diff 結果須人工核對 submodule HEAD"
    has_pseudo_diff_risk=1
  fi
fi
echo

# D-2: core.autocrlf
echo "### D-2: core.autocrlf 偵測"
ACR="$(git config --get core.autocrlf 2>/dev/null || echo UNSET)"
echo "core.autocrlf: $ACR"
if [ "$ACR" = "UNSET" ] || [ "$ACR" = "false" ] || [ "$ACR" = "input" ]; then
  echo "結論: 不主動轉換 CRLF，不會因 core.autocrlf 產生假性 diff"
elif [ "$ACR" = "true" ]; then
  echo "[WARN] core.autocrlf=true，commit 時可能主動把 LF→CRLF 轉換產生假性 diff"
  has_pseudo_diff_risk=1
fi
echo

# D-3: .gitattributes
echo "### D-3: .gitattributes 偵測"
if [ ! -e "$ROOT/.gitattributes" ]; then
  echo "狀態: 不存在"
  echo "結論: 本 repo 無 .gitattributes 統一 eol 設定；CRLF 行為完全由 core.autocrlf 控制"
else
  if [ ! -s "$ROOT/.gitattributes" ]; then
    echo "狀態: 存在但為空檔"
    echo "結論: .gitattributes 空檔無有效設定，CRLF 行為完全由 core.autocrlf 控制"
  else
    echo "內容:"
    sed 's/^/    /' "$ROOT/.gitattributes"
    EOL_PATTERNS="$(grep -E '^\*\.([a-zA-Z0-9]+)\s+(text|binary)|^.*\s+eol=(lf|crlf|native)' "$ROOT/.gitattributes" || true)"
    if [ -n "$EOL_PATTERNS" ]; then
      echo "結論: .gitattributes 有 eol 設定："
      printf '%s\n' "$EOL_PATTERNS" | sed 's/^/    /'
    else
      echo "結論: .gitattributes 存在但無 eol 相關設定"
    fi
  fi
fi
echo

# D-4: untracked 偵測（排除 .gitignore 已忽略的）
echo "### D-4: untracked 偵測（git ls-files --others --exclude-standard）"
UNT_OUT="$(git ls-files --others --exclude-standard 2>&1)"
UNT_RC=$?
if [ "$UNT_RC" = "0" ]; then
  if [ -z "$UNT_OUT" ]; then
    echo "(空)"
    echo "結論: 無未追蹤檔案"
  else
    echo "未追蹤檔案:"
    printf '%s\n' "$UNT_OUT" | sed 's/^/    /'
    echo "[WARN] 有未追蹤檔案，可能污染後續 diff 證據鏈"
    has_pseudo_diff_risk=1
  fi
else
  echo "exit: $UNT_RC（命令失敗）"
fi
echo

# D-5: 假性 diff 工具旗標盤點（不施加，只記錄哪些可被使用）
echo "### D-5: 假性 diff 工具旗標盤點（依 architect 決策，不施加，僅備查）"
cat <<'EOF'
- submodule dirty:    --ignore-submodules=dirty / untracked（不施加）
- CRLF / eol:         --ignore-cr-at-eol / --ignore-space-at-eol / --ignore-all-space（不施加）
- 空行:                --ignore-blank-lines（不施加）
依決策：腳本不加任何 --ignore-* 旗標，避免掩蓋真問題。
       若未來發現真假性 diff，應回到 .gitattributes / core.autocrlf 政策層修補。
EOF
echo

# --- 階段 E：總體結論 ---
echo "## 階段 E：總體結論"
echo
echo "聚合計數:"
echo "  fail=$fail  fetch_failed=$fetch_failed  pseudo_diff_risk=$has_pseudo_diff_risk"
echo

# 結論分流
if [ "$fetch_failed" = "1" ]; then
  echo "**[FETCH 失敗] 比對結果作廢**：origin/main 是過時 ref，後續 diff/hash 證據不可信"
  echo "建議動作：排查網路/遠端權限後重跑"
elif [ "$fail" = "0" ] && [ "$has_pseudo_diff_risk" = "0" ]; then
  echo "**[空 diff]** 4 條驗證命令全綠、fetch 成功、假性 diff 偵測無風險"
  echo "結論：當前工作樹與 origin/main 處於空 diff 狀態（HEAD hash 完全一致）"
elif [ "$fail" = "0" ] && [ "$has_pseudo_diff_risk" = "1" ]; then
  echo "**[空 diff + 假性 diff 風險]**: 4 條命令全綠但偵測到潛在假性 diff 源（submodule/CRLF/untracked）"
  echo "建議動作：人工核對階段 D 各項輸出，確認風險可接受後再關閉"
else
  echo "**[非空 diff]**: 4 條驗證命令未全綠，詳見上方 [FAIL] 標記"
  echo "建議動作：依上方 [FAIL] 標記逐條排查後重跑"
fi
echo
echo "報告已寫入：$OUT"
echo "============================================================"

# 確保 tee pipeline 在 exit 前 flush（保險：等背景 tee 子行程收 EOF）
wait 2>/dev/null || true
exit "$fail"
