#!/usr/bin/env bash
# ============================================================================
# scan_shell_usage.sh — shell 用法靜態掃描（唯一事實來源 / SSOT）
# ----------------------------------------------------------------------------
# 用途：偵測潛在的 shell 注入面，命中後印出警告。
#   1) `subprocess.run(..., shell=True)` 等 → Ruff S602/S604/S605
#   2) `asyncio.create_subprocess_shell(...)` → ripgrep/grep 補掃
#      （S 規則不抓 create_subprocess_shell，因它「天生就是 shell」、無 shell= 參數）
#
# 介面契約（改名／改行為時請同步 CI、pre-commit、CONTRIBUTING.md）：
#   呼叫：   bash scripts/scan_shell_usage.sh [掃描目標...]
#   預設目標：studio（不掃 ./ 與 tests/，避免日後升 block 被既存樣本卡住）
#   環境變數 SCAN_MODE：
#     warn  （預設）→ 永遠 exit 0，僅印警告，絕不阻斷 CI/commit
#     block         → 有任何命中則 exit 1
#   需求：bash + ruff；ripgrep(rg) 可選，缺則自動 fallback 到 grep。
#
# 升級為 blocking：單處設定 SCAN_MODE=block 即可（CI/pre-commit/本機皆然）。
# ============================================================================
set -euo pipefail

SCAN_MODE="${SCAN_MODE:-warn}"
# 掃描目標：位置參數優先，否則預設 studio。
if [ "$#" -gt 0 ]; then
  TARGETS=("$@")
else
  TARGETS=("studio")
fi

RUFF_RULES="S602,S604,S605"
hits=0

echo "== shell 用法掃描 (SCAN_MODE=${SCAN_MODE}) 目標: ${TARGETS[*]} =="

# ---- 1) Ruff S602/S604/S605：shell=True 類 -------------------------------
# --isolated 徹底隔絕主 pyproject 的 select/ignore/exclude/per-file-ignores，
# 確保此規則集獨立、不汙染也不被汙染。|| true 吞掉 ruff 命中時的非零退出，
# 由本腳本最後統一依 SCAN_MODE 決定 exit code。
echo "-- [1/2] Ruff ${RUFF_RULES} (shell=True 類) --"
ruff_out="$(ruff check --isolated --select "${RUFF_RULES}" --output-format concise "${TARGETS[@]}" 2>&1 || true)"
if printf '%s\n' "$ruff_out" | grep -Eq 'S60[245]'; then
  printf '%s\n' "$ruff_out"
  hits=1
else
  echo "（無 S602/S604/S605 命中）"
fi

# ---- 2) create_subprocess_shell：rg 優先、grep 退路 ----------------------
echo "-- [2/2] create_subprocess_shell (grep 補掃) --"
if command -v rg >/dev/null 2>&1; then
  grep_out="$(rg -n --no-heading -g '*.py' 'create_subprocess_shell' "${TARGETS[@]}" 2>/dev/null || true)"
else
  grep_out="$(grep -rn --include='*.py' 'create_subprocess_shell' "${TARGETS[@]}" 2>/dev/null || true)"
fi
if [ -n "$grep_out" ]; then
  printf '%s\n' "$grep_out"
  hits=1
else
  echo "（無 create_subprocess_shell 命中）"
fi

# ---- exit code 收斂 ------------------------------------------------------
echo "== 掃描完成 =="
if [ "$SCAN_MODE" = "block" ] && [ "$hits" -ne 0 ]; then
  echo "SCAN_MODE=block 且有命中 → 失敗 (exit 1)" >&2
  exit 1
fi
# warn 模式：恆回 0，不阻斷。
exit 0
