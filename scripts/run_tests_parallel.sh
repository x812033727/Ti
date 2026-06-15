#!/usr/bin/env bash
# 把測試套件切兩組平行跑、合併 exit code，避免單進程 wall-time 過長。
# 無新增依賴（不依賴 pytest-xdist），僅用 shell 背景程序。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 99
LOG="${TMPDIR:-/tmp}"

python3 -m pytest tests/core tests/autopilot tests/server \
  -q -p no:cacheprovider -o addopts="" > "$LOG/ptA.log" 2>&1 &
PA=$!
python3 -m pytest tests/docs tests/publish tests/scan tests/sandbox \
  tests/export tests/deploy tests/settings tests/*.py \
  -q -p no:cacheprovider -o addopts="" > "$LOG/ptB.log" 2>&1 &
PB=$!

wait "$PA"; RA=$?
wait "$PB"; RB=$?

echo "[groupA exit=$RA] $(tail -n1 "$LOG/ptA.log")"
echo "[groupB exit=$RB] $(tail -n1 "$LOG/ptB.log")"
[ "$RA" -eq 0 ] && [ "$RB" -eq 0 ]
exit $?
