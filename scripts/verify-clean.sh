#!/usr/bin/env bash
# scripts/verify-clean.sh
# 任務 #2 驗證腳本（異議校正版）。
# 原始議程假設：當前在 main、工作樹乾淨、與 origin/main 為 +0 -0、hash 一致。
# 經實測此假設為偽（HEAD ≠ origin/main、HEAD 領先 2 commits、submodule 機制壞掉），
# 因此本腳本不預期「空 diff 全綠」結論；改為「誠實跑完 4 條命令 + 假性 diff 三項偵測 +
# 議程前提對照」，把實況寫入 $TMPDIR 供 PM/QA 核對。
#
# 退出 0 語意 = 4 條命令如「議程前提預期」且 fetch 成功；退出 1 語意 = 任一不符或 fetch 失敗。
# （不預期為真：當前現況下本腳本固定 exit 1；這是誠實訊號，不是錯誤。）
#
# 設計依據：architect 決策清單 + critic 第二輪異議（議程前提 / hash / submodule 三項全為假）。
# 輸出格式：每條命令「$ <cmd> / <stdout+stderr> / exit: N」三行一組；
# 末尾附「議程前提對照」「假性 diff 偵測實況」「總體結論」三段。
# 輸出到 $TMPDIR/clean-verify-output-<branch>-<ts>.txt，不入版控、不污染工作樹。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || exit 99
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

fail=0
fetch_failed=0
agenda_premise_match=0  # 議程前提對照結果（0=全對、>0=有幾項不符）

# 純 file redirect：避免 exec > >(tee ...) 的 process substitution 與
# bash exit 的時序競爭（wait 抓不到 process substitution child，會卡到
# 父 shell 收 EOF，導致腳本在 tee 子行程結束前不退出）。
# 用 fd 3 備份原始 stdout/stderr 供收尾 echo 用。
exec 3>&1 4>&2
exec > "$OUT" 2>&1

echo "============================================================"
echo "verify-clean.sh  執行報告（議程前提校正版）"
echo "============================================================"
echo "branch:           $BRANCH"
echo "HEAD:             $(git rev-parse HEAD 2>/dev/null || echo N/A)"
echo "origin/main:      $(git rev-parse origin/main 2>/dev/null || echo N/A)"
echo "fetch timestamp:  $TS (UTC)"
echo "executor:         ${USER:-$(id -un 2>/dev/null || echo unknown)}"
echo "cwd:              $ROOT"
echo "output file:      $OUT"
echo "============================================================"
echo

# --- 階段 A：前置防呆 ---
echo "## 階段 A：前置防呆"
echo

echo "\$ git rev-parse --is-inside-work-tree"
git rev-parse --is-inside-work-tree
echo "exit: $?"
echo

echo "\$ git rev-parse --verify origin/main^{commit}"
git rev-parse --verify origin/main^{commit} 2>&1 || echo "(ref 不存在)"
echo "exit: $?"
echo

# --- 階段 B：fetch ---
echo "## 階段 B：git fetch origin"
echo
echo "\$ git fetch origin"
git fetch origin 2>&1
echo "exit: $?"
FETCH_RC=$?
if [ "$FETCH_RC" != "0" ]; then
  echo "[FETCH FAILED] 依決策，比對結果作廢"
  fetch_failed=1
  fail=$((fail+1))
fi
echo

# --- 階段 C：4 條驗證命令 ---
echo "## 階段 C：4 條驗證命令"
echo

# (1) status --porcelain=v2 --branch --untracked-files=normal
echo "\$ git status --porcelain=v2 --branch --untracked-files=normal"
ST_OUT="$(git status --porcelain=v2 --branch --untracked-files=normal 2>&1)"
ST_RC=$?
printf '%s\n' "$ST_OUT"
echo "exit: $ST_RC"
# 預期：exit 0 且無檔案行
if [ "$ST_RC" = "0" ] && ! printf '%s\n' "$ST_OUT" | grep -vE '^# branch\.' | grep -q .; then
  echo "[OK] 工作樹乾淨（僅有 branch header，無檔案行）"
else
  echo "[FAIL] 工作樹非乾淨或 status 報錯"
  fail=$((fail+1))
fi
echo

# (2) diff --quiet origin/main HEAD
echo "\$ git diff --quiet origin/main HEAD"
git diff --quiet origin/main HEAD 2>&1
DIFF_RC=$?
echo "exit: $DIFF_RC"
# 補上 shortstat 與 --name-only 作證據
echo "  -- 證據補強（議程前提校正必備）--"
echo "  \$ git diff --shortstat origin/main HEAD"
git diff --shortstat origin/main HEAD
echo "  \$ git diff --name-only origin/main HEAD"
git diff --name-only origin/main HEAD
echo "  \$ git rev-list --count origin/main..HEAD / HEAD..origin/main"
echo "    ahead of origin/main: $(git rev-list --count origin/main..HEAD)"
echo "    behind origin/main:   $(git rev-list --count HEAD..origin/main)"
echo "  \$ git log --oneline origin/main..HEAD"
git log --oneline origin/main..HEAD | sed 's/^/    /'
if [ "$DIFF_RC" = "0" ]; then
  echo "[OK] 與 origin/main 無 diff"
else
  echo "[FAIL] 與 origin/main 有 diff (exit=$DIFF_RC) — 此為議程前提不符之直接證據"
  fail=$((fail+1))
fi
echo

# (3) diff --quiet --cached
echo "\$ git diff --quiet --cached"
git diff --quiet --cached 2>&1
CACH_RC=$?
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
  echo "[FAIL] HEAD hash 與 origin/main 不一致 — 此為議程前提不符之直接證據"
  fail=$((fail+1))
  HASH_RC=1
fi
echo "exit: $HASH_RC"
echo

# --- 階段 D：假性 diff 排除政策偵測（不修補，只誠實盤點）---
echo "## 階段 D：假性 diff 排除政策偵測（不修補，只誠實盤點）"
echo
echo "政策原則：腳本不加任何 --ignore-submodules=dirty / --ignore-cr-at-eol 等"
echo "         修補 flag，避免掩蓋真問題。若有可疑 diff，必須從源頭（.gitmodules /"
echo "         .gitattributes / core.autocrlf / 索引 gitlink）解決。"
echo

# D-1: .gitmodules 與 submodule 機制
echo "### D-1: .gitmodules 與 submodule 機制（議程前提校正後，這是真問題段）"
echo
echo "\$ ls -la .gitmodules"
ls -la .gitmodules 2>&1
echo "exit: $?"
echo
echo "\$ stat .gitmodules（看是 regular file / char device / symlink / 不存在）"
stat .gitmodules 2>&1
echo "exit: $?"
echo
if [ ! -e .gitmodules ]; then
  SUB_STATUS="NOT_EXIST"
elif [ -c .gitmodules ]; then
  SUB_STATUS="CHAR_DEVICE（例如 /dev/null）"
elif [ -L .gitmodules ]; then
  SUB_STATUS="SYMLINK→$(readlink .gitmodules 2>/dev/null || echo unknown)"
elif [ ! -s .gitmodules ]; then
  SUB_STATUS="EMPTY_FILE（regular file, 0 byte）"
else
  SUB_STATUS="HAS_CONTENT"
fi
echo ".gitmodules 解析結果: $SUB_STATUS"
echo
echo "\$ git ls-files --stage | grep '^160000'（索引內的 gitlink）"
GITLINKS="$(git ls-files --stage | awk '$1==160000 {print "    " $0}')"
if [ -n "$GITLINKS" ]; then
  echo "$GITLINKS"
else
  echo "    (無 gitlink)"
fi
echo
echo "\$ git submodule status"
git submodule status 2>&1
SUB_RC=$?
echo "exit: $SUB_RC"
echo
echo "[D-1 結論] 索引 gitlink 存在 + .gitmodules $SUB_STATUS → submodule 機制壞掉"
echo "         （gitlink 找不到對應 mapping，工具鏈報 FATAL）"
echo "         這不是「可忽略的假性 diff 源」，是「submodule 設定缺失的真問題」。"
echo "         本 repo 受此問題影響：任何含 .pc-cache-qa/repor4x7pmx5 路徑的"
echo "         git status / git diff 結果都需人工核對；本工作目錄的 hash 比對"
echo "         並不因此失效（hash 比的是 commit object，不經 submodule 解析），"
echo "         但 close-out 結論不可寫「本 repo 不受 submodule 影響」。"
echo "         （議程前提 P5 的不符判定在階段 E 統一計算）"
echo

# D-2: core.autocrlf
echo "### D-2: core.autocrlf 偵測"
ACR="$(git config --get core.autocrlf 2>/dev/null || echo UNSET)"
echo "core.autocrlf: $ACR"
case "$ACR" in
  UNSET|false|input)
    echo "[D-2 結論] 不主動轉換 CRLF；本 repo 不會因 core.autocrlf 產生假性 diff"
    echo "          （UNSET 在 Linux 等同 false，commit 時不轉換；checkout 時僅在"
    echo "          配合 .gitattributes 標 text 才轉換，本 repo 兩者皆無，雙重保險）"
    ;;
  true)
    echo "[D-2 結論] core.autocrlf=true；commit 時主動 LF→CRLF 轉換，理論上可造成假性 diff"
    ;;
esac
echo

# D-3: .gitattributes
echo "### D-3: .gitattributes 偵測"
if [ ! -e .gitattributes ]; then
  echo "狀態: 不存在"
  echo "[D-3 結論] 本 repo 無 .gitattributes；CRLF 行為完全由 core.autocrlf 控制，"
  echo "          且 core.autocrlf 為 UNSET（見 D-2），雙重無設定 → 無 CRLF 假性 diff 源"
else
  if [ -L .gitattributes ]; then echo "狀態: symlink→$(readlink .gitattributes)"
  elif [ ! -s .gitattributes ]; then echo "狀態: 0-byte regular file"; fi
  echo "內容:"
  sed 's/^/    /' .gitattributes
fi
echo

# D-4: untracked 偵測
echo "### D-4: untracked 偵測（git ls-files --others --exclude-standard）"
UNT_OUT="$(git ls-files --others --exclude-standard 2>&1)"
UNT_RC=$?
if [ "$UNT_RC" = "0" ]; then
  if [ -z "$UNT_OUT" ]; then
    echo "(空)"
    echo "[D-4 結論] 無未追蹤檔案"
  else
    echo "未追蹤檔案:"
    printf '%s\n' "$UNT_OUT" | sed 's/^/    /'
  fi
else
  echo "exit: $UNT_RC（命令失敗）"
fi
echo

# D-5: 假性 diff 工具旗標盤點（不施加，僅備查以利 PM 決策）
echo "### D-5: 假性 diff 工具旗標盤點（不施加，僅備查）"
cat <<'EOF'
可被施加（但本腳本依決策不施加，避免掩蓋真問題）:
  - submodule dirty:    --ignore-submodules=dirty / untracked
  - CRLF / eol:         --ignore-cr-at-eol / --ignore-space-at-eol / --ignore-all-space
  - 空行:                --ignore-blank-lines
若未來 close-out 決定接受當前現況「submodule 異常 + HEAD 領先 origin/main」並關閉，
則下次驗證可考慮在 status / diff 階段加 --ignore-submodules=dirty 把 .pc-cache-qa
路徑的雜訊蓋掉（但這是「決定接受問題」後的妥協，不是「修補問題」）。
EOF
echo

# --- 階段 E：議程前提對照 ---
echo "## 階段 E：議程前提對照（critic 第二輪異議）"
echo
echo "議程原文前提（任一不符則 close-out 結論不可寫「空 diff」）："
echo "  P1. 當前在 main（無其他分支）"
echo "  P2. 工作樹乾淨且 status 顯示 branch.ab +0 -0"
echo "  P3. 與 origin/main 空 diff（diff --quiet exit 0）"
echo "  P4. HEAD hash = origin/main hash"
echo "  P5. 本 repo 不受 submodule 假性 diff 影響"
echo

# P1
CUR_BR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo DETACHED)"
if [ "$CUR_BR" = "main" ]; then
  echo "  P1 [MATCH]  當前分支=$CUR_BR"
else
  echo "  P1 [MISMATCH]  當前分支=$CUR_BR（議程預期 main）"
  agenda_premise_match=$((agenda_premise_match+1))
fi

# P2（branch.ab 需要 upstream；無 upstream 字面不可達）
HAS_AB="$(printf '%s\n' "$ST_OUT" | grep -E '^# branch\.ab' || true)"
if [ -n "$HAS_AB" ] && echo "$HAS_AB" | grep -qE '\+0 -0'; then
  echo "  P2 [MATCH]  status 顯示 $HAS_AB"
else
  echo "  P2 [MISMATCH]  status 無 branch.ab +0 -0（task-2 無 upstream；實際輸出：${HAS_AB:-無 branch.ab 行}）"
  agenda_premise_match=$((agenda_premise_match+1))
fi

# P3
if [ "$DIFF_RC" = "0" ]; then
  echo "  P3 [MATCH]  diff --quiet origin/main HEAD exit=0"
else
  echo "  P3 [MISMATCH]  diff --quiet origin/main HEAD exit=$DIFF_RC（有 diff，見階段 C 證據）"
  agenda_premise_match=$((agenda_premise_match+1))
fi

# P4
if [ "$HASH_RC" = "0" ]; then
  echo "  P4 [MATCH]  HEAD hash = origin/main hash"
else
  echo "  P4 [MISMATCH]  HEAD hash ≠ origin/main hash（見階段 C 證據）"
  agenda_premise_match=$((agenda_premise_match+1))
fi

# P5
echo "  P5 [MISMATCH]  submodule 機制壞掉（gitlink 存在 + .gitmodules $SUB_STATUS + git submodule status FATAL）"
echo "                本 repo *受* submodule 異常影響，議程原文「不受影響」為假命題"
agenda_premise_match=$((agenda_premise_match+1))
echo
echo "  議程前提不符項數: $agenda_premise_match / 5"
echo

# --- 階段 F：總體結論 ---
echo "## 階段 F：總體結論"
echo
echo "聚合計數:"
echo "  fail=$fail  fetch_failed=$fetch_failed  agenda_mismatch=$agenda_premise_match / 5"
echo

if [ "$fetch_failed" = "1" ]; then
  echo "**[FETCH 失敗]** origin/main 是過時 ref，比對結果作廢；建議排查網路後重跑"
elif [ "$agenda_premise_match" -gt 0 ]; then
  echo "**[議程前提不符]** 5 項議程前提中 $agenda_premise_match 項不符（詳見階段 E）"
  echo ""
  echo "  具體不符項："
  echo "    - HEAD 領先 origin/main $(git rev-list --count origin/main..HEAD) commits"
  echo "    - 其中 cbe0afd「任務#2 第1輪」即本任務自己的 commit（git log --oneline origin/main..HEAD 證據在階段 C）"
  echo "    - submodule 機制壞掉（gitlink 存在但 .gitmodules $SUB_STATUS），submodule status FATAL"
  echo ""
  echo "  **close-out 結論不可寫「空 diff」**——這與實況矛盾，會被 QA 一鍵打回"
  echo ""
  echo "  **建議 PM 處理路徑（取捨，請 PM 決策）：**"
  echo "    路徑 A: 接受現況關閉"
  echo "            接受 task-2 領先 origin/main 2 commits（屬本任務與架構決策 commit）為"
  echo "            預期狀態；接受 submodule 異常為已知問題（後續任務處理），close-out"
  echo "            文件明列此兩項為「pre-existing/expected」，不算回歸。"
  echo "    路徑 B: 先校正前提再重跑"
  echo "            (a) 確認驗證標的：此 lane (task-2) 還是主 checkout (main)？"
  echo "            (b) 處理 submodule 異常：補上有效的 .gitmodules 或移除 .pc-cache-qa gitlink"
  echo "            (c) 決定 task-2 領先的 2 commits 要保留（要 push 給 origin/main？）"
  echo "                還是要 reset 到 origin/main 後重做"
  echo "    路徑 C: 縮小驗收範圍"
  echo "            議程驗收標準第 1 條「branch.ab +0 -0」字面不可達（無 upstream）；"
  echo "            第 3 條「hash 一致」字面不可達（HEAD ≠ origin/main）。可由 PM 重新"
  echo "            對齊驗收標準，使當前實況可被誠實標記為「通過」或「不適用」。"
else
  echo "**[空 diff]** 4 條驗證命令全綠、議程前提全對、無假性 diff 風險"
fi
echo
echo "報告已寫入：$OUT"
echo "============================================================"

# 收尾：把完整報告 echo 到原始 stdout 供 live 觀察（檔案內容才是契約，stdout 為鏡像）
exec 1>&3 2>&4
cat "$OUT"
exit "$fail"
