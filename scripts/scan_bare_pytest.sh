#!/usr/bin/env bash
# ============================================================================
# scan_bare_pytest.sh — 裸 pytest 指令靜態掃描（唯一事實來源 / SSOT）
# ----------------------------------------------------------------------------
# 用途：偵測文件中「裸 pytest」執行指令（如 `pytest tests/`），命中後印出
#       友善訊息指引改用 `python -m pytest`。與 scan_shell_usage.sh 同家族。
#
# 介面契約（改名／改行為時請同步 CI、.pre-commit-config.yaml）：
#   呼叫：   bash scripts/scan_bare_pytest.sh [掃描目標...]
#   預設目標：docs（無位置參數時）。pre-commit 以 pass_filenames 傳變動檔。
#   環境變數 SCAN_MODE：
#     block （預設）→ 有任何命中則 exit 1（符合驗收標準#2）
#     warn          → 永遠 exit 0，僅印警告（家族逃生口，維持介面一致）
#   需求：bash；ripgrep(rg) 可選，缺則自動 fallback 到 grep。
#
# === 正則策略（務必遵守，禁止改回 lookbehind/PCRE）=========================
# 引擎限 ERE（rg 預設引擎 / grep -E 皆支援），「禁用」lookbehind 與 PCRE，
# 否則 grep fallback 在無 GNU grep -P 的環境會炸掉。
# 執行順序固定為「兩段式」，順序不可顛倒：
#   第一段：抓黑樣式候選行（裸 pytest 指令）
#   第二段：從候選中「濾除」白名單行（-m pytest / run pytest / @pytest / pytest.）
# 黑樣式左邊界用 (^|[^.a-zA-Z-])，故反引號 inline code 內的 pytest 也會命中；
# 右邊界要求後接指令型參數（tests/ 、-[a-z]、*.py 路徑）以避免誤殺行內套件名。
# ============================================================================
set -euo pipefail

SCAN_MODE="${SCAN_MODE:-block}"

# 掃描目標：位置參數優先，否則預設 docs。
if [ "$#" -gt 0 ]; then
  TARGETS=("$@")
else
  TARGETS=("docs")
fi

# 黑樣式：裸 pytest 當執行指令（左邊界非 . 字母 -，右邊界須接指令型參數）。
PATTERN_BLACK='(^|[^.a-zA-Z-])pytest[[:space:]]+(tests?/|-[a-z]|[^[:space:]]+\.py)'
# 白名單：整行含任一即放行（合法寫法）。
PATTERN_WHITE='(-m[[:space:]]+pytest|run[[:space:]]+pytest|@pytest|pytest\.)'

echo "== 裸 pytest 掃描 (SCAN_MODE=${SCAN_MODE}) 目標: ${TARGETS[*]} =="

# ---- 第一段：抓黑樣式候選（rg 優先、grep 退路）---------------------------
# -H/--with-filename 強制輸出檔名，確保單檔/多檔皆為穩定的 file:line:content 格式。
if command -v rg >/dev/null 2>&1; then
  raw="$(rg -nH --no-heading -e "$PATTERN_BLACK" "${TARGETS[@]}" 2>/dev/null || true)"
else
  raw="$(grep -rnHE -e "$PATTERN_BLACK" "${TARGETS[@]}" 2>/dev/null || true)"
fi

# ---- 第二段：濾除白名單行 -------------------------------------------------
if [ -n "$raw" ]; then
  matches="$(printf '%s\n' "$raw" | grep -Ev "$PATTERN_WHITE" || true)"
else
  matches=""
fi

# ---- 輸出友善訊息 + exit code 收斂 ---------------------------------------
hits=0
if [ -n "$matches" ]; then
  hits=1
  # rg/grep 輸出格式皆為 file:line:content，只取前兩欄轉成友善訊息。
  printf '%s\n' "$matches" | while IFS= read -r line; do
    [ -z "$line" ] && continue
    file="${line%%:*}"
    rest="${line#*:}"
    lineno="${rest%%:*}"
    echo "${file}:${lineno}: 偵測到裸 pytest 指令，請改用 python -m pytest（例：python -m pytest tests/）" >&2
  done
fi

echo "== 掃描完成 =="
if [ "$SCAN_MODE" = "block" ] && [ "$hits" -ne 0 ]; then
  echo "SCAN_MODE=block 且有命中 → 失敗 (exit 1)" >&2
  exit 1
fi
# warn 模式：恆回 0，不阻斷。
exit 0
