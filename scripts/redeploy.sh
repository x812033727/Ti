#!/usr/bin/env bash
# 後備重新佈署腳本：拉取主 repo 最新 main 後重啟服務。
# 用於 POST /api/redeploy 不可用時（外部排程 / 人工執行）。
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[redeploy] pulling latest main..."
git pull --ff-only
echo "[redeploy] restarting studio.server..."
exec python -m studio.server
