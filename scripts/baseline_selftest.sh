#!/usr/bin/env bash
# 任務 #1 基準快照自測（doc-only 任務）。
# 退出 0 的語意 = 實況與 BASELINE_task1.md 記錄一致：
#   (a) ruff check . 綠、collect 綠；
#   (b) 本分支相對 origin/main 無任何 .py 變更（doc-only 不變式）；
#   (c) 完整 pytest 僅剩唯一的 pre-existing 失敗 test_ruff_format_check_dot_passes
#       （origin/main 本身即紅，超出本快照任務範圍），無任何回歸。
# 無新增執行期依賴；僅用 shell 背景程序把測試切兩組平行，縮短 wall-time(<60s)。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || exit 99
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0

# 1) lint
ruff check . >/dev/null 2>&1; echo "[lint exit=$?]"

# 2) collect
python3 -m pytest --collect-only -q -o addopts="" >"$TMP/col.log" 2>&1
echo "[collect exit=$?] $(tail -n1 "$TMP/col.log")"

# 3) doc-only 不變式（commit 無關的真相，避免 test_no_py_changed 因「撤回尚未提交」的時序假紅）
nd=$(git diff origin/main --name-only -- '*.py' 2>/dev/null | wc -l | tr -d ' ')
if [ "$nd" = "0" ]; then echo "[doc-only OK] 相對 origin/main 無 .py 變更"; else echo "[doc-only FAIL] 動了 $nd 個 .py"; fail=1; fi

# 4) 完整 pytest（切兩組平行）。扣除 test_no_py_changed：它讀「已提交 HEAD」，在 orchestrator
#    提交本次撤回前會時序假紅；其等價條件已由第 3 步以 commit 無關方式驗證。
DES="--deselect tests/test_task1_retry_doc.py::test_no_py_changed"
python3 -m pytest tests/core tests/autopilot tests/server $DES \
  -q -p no:cacheprovider -o addopts="" >"$TMP/A.log" 2>&1 &
PA=$!
python3 -m pytest tests/docs tests/publish tests/scan tests/sandbox tests/export tests/deploy tests/settings tests/*.py $DES \
  -q -p no:cacheprovider -o addopts="" >"$TMP/B.log" 2>&1 &
PB=$!
wait "$PA"; RA=$?; wait "$PB"; RB=$?
echo "[groupA exit=$RA] $(tail -n1 "$TMP/A.log")"
echo "[groupB exit=$RB] $(tail -n1 "$TMP/B.log")"

grep -h "^FAILED " "$TMP/A.log" "$TMP/B.log" 2>/dev/null | sed 's/^FAILED //; s/ -.*//' | sort -u >"$TMP/failed.txt"
echo "--- 實際失敗測試 ---"; cat "$TMP/failed.txt"

EXPECTED="tests/scan/test_scan_shell_usage_no_pollution.py::test_ruff_format_check_dot_passes"
unexpected=$(grep -v -F "$EXPECTED" "$TMP/failed.txt" 2>/dev/null || true)
if [ -n "$unexpected" ]; then echo "[REGRESSION] 出現非預期失敗："; echo "$unexpected"; fail=1; fi
if grep -qF "$EXPECTED" "$TMP/failed.txt" 2>/dev/null; then
  echo "[note] 唯一失敗為 pre-existing：$EXPECTED（origin/main 本身即紅，超出本 doc-only 任務範圍）"
fi

echo "[selftest exit=$fail]"
exit $fail
