#!/bin/bash
# Ti autopilot 外置 kill switch（watchdog）：獨立於 autopilot 程序的安全底線。
#
# 行為：每次執行對 HEALTH_URL 做一次健康檢查——成功即把失敗計數歸零；失敗把計數 +1
# 落到 STATE_FILE；連續失敗達 THRESHOLD 且 pause 檔不存在時，寫 PAUSE_FILE 讓
# autopilot 主迴圈停止接新任務（config.autopilot_paused() 會偵測此檔）。
# 恢復需人工 rm PAUSE_FILE（或網頁 Autopilot 面板「恢復」）——kill switch 語意即
# 「異常時停下等人看」，不自動復位。
#
# 外置契約（勿破壞）：
#   1. 本腳本的設定走 TI_WATCHDOG_* 環境變數，刻意「不」經 studio/config.py 或
#      settings.py——autopilot 能自改的設定通道（UI 寫 .env / config.reload）
#      一律碰不到本腳本的目標路徑，守住「驗證器不可被受監控者自改」的獨立性。
#   2. 只用 bash 內建 + curl，不依賴被監控對象的 Python runtime、不用 sed/awk/PCRE
#      （可攜性：fallback 環境可能沒有）。
#   3. 對應契約測試：tests/autopilot/test_external_killswitch_contract.py。
#
# 安裝（搭配 deploy/ti-watchdog.service + ti-watchdog.timer，每 5 分鐘一次）：
#   cp /opt/ti/deploy/ti-watchdog.{sh,service,timer} 就位後
#   systemctl daemon-reload && systemctl enable --now ti-watchdog.timer
set -u

HEALTH_URL="${TI_WATCHDOG_HEALTH_URL:-http://127.0.0.1:8021/api/health}"
PAUSE_FILE="${TI_WATCHDOG_PAUSE_FILE:-/opt/ti/AUTOPILOT_PAUSED}"
STATE_FILE="${TI_WATCHDOG_STATE_FILE:-/var/lib/ti-watchdog/failures}"
THRESHOLD="${TI_WATCHDOG_THRESHOLD:-3}"

mkdir -p "$(dirname "$STATE_FILE")"

if curl -fsS --max-time 10 "$HEALTH_URL" > /dev/null 2>&1; then
  : > "$STATE_FILE"   # 健康：失敗計數歸零
  exit 0
fi

count=0
[ -s "$STATE_FILE" ] && read -r count < "$STATE_FILE"
case "$count" in *[!0-9]*) count=0 ;; esac   # 壞計數容錯歸零（純 bash，不用外部工具）
count=$((count + 1))
printf '%s\n' "$count" > "$STATE_FILE"

if [ "$count" -ge "$THRESHOLD" ] && [ ! -e "$PAUSE_FILE" ]; then
  {
    printf 'watchdog: health check failed %s consecutive times (%s)\n' "$count" "$HEALTH_URL"
    date
  } > "$PAUSE_FILE"
fi
