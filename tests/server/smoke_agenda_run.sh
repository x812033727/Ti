#!/usr/bin/env bash
# 任務 #5 QA 冒煙外層：同一命令內啟動真實 server → 健康檢查 → 跑 client → kill。
# （沙箱跨命令 localhost 不互通，故 server 與 client 必須同命令啟動。）
set -u
PORT="${1:-8901}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="${TMPDIR:-/tmp}/smoke5_server.log"

# 注意：宿主環境可能已設 TI_ACCESS_PASSWORD（部署用），冒煙必須明確清空門禁。
env TI_ACCESS_PASSWORD= TI_OFFLINE=1 TI_OFFLINE_DELAY=0 TI_DISCUSS_MODE=round_robin TI_AGENDA_ROUNDS=1 \
    TI_DEBATE_ROUNDS=1 TI_PARALLEL_TASKS=0 TI_HUDDLE=0 TI_REFLEXION=0 \
    TI_SELF_REFINE_ITERS=0 TI_OBJECTIVE_GATE=0 TI_ADR=0 TI_PUBLISH_AUTO=0 TI_PORT="$PORT" \
    TI_WORKSPACE_ROOT="${TMPDIR:-/tmp}/smoke5/ws" TI_HISTORY_ROOT="${TMPDIR:-/tmp}/smoke5/hist" \
    python3 -m studio.server > "$LOG" 2>&1 &
SRV=$!

ok=0
for _ in $(seq 1 40); do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then ok=1; break; fi
  sleep 0.5
done
if [ "$ok" != 1 ]; then
  echo "SERVER BOOT FAIL"; tail -20 "$LOG"; kill "$SRV" 2>/dev/null; exit 2
fi
echo "SERVER UP (pid $SRV)"

timeout 120 python3 "$ROOT/tests/server/smoke_agenda_real_server.py" "$PORT"
RC=$?

kill "$SRV" 2>/dev/null
wait "$SRV" 2>/dev/null
exit "$RC"
