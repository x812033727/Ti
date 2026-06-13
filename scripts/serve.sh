#!/usr/bin/env bash
# serve.sh — 唯一啟動入口（可攜直譯器偵測）。
# 背景：驗證環境可能只有 python3 而無 python（或反之）；宣告裸 `python -m studio.server`
# 在這類環境直接 command not found。本包裝偵測可用直譯器後啟動，之後的「執行指令」
# 宣告一律引用本腳本（或明寫 python3），禁止再出現裸 `python` 前綴。
# 用法：bash scripts/serve.sh [uvicorn 參數透傳]
set -eu
cd "$(dirname "$0")/.."

PY="$(command -v python3 || command -v python)" || {
  echo "serve.sh: 找不到 python3 或 python，請先安裝 Python 3" >&2
  exit 127
}
exec "$PY" -m studio.server "$@"
