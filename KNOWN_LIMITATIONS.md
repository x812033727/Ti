# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 改寫層3監控判死核心，逐條對齊 liveness_verdict 規則 1–5，含 --self-test 黑白樣本，落檔 `deploy/ti-layer3-monitor.sh` 入版控
- [ ] 盤點生產心跳基線：確認 `/api/autopilot` 的 `heartbeat` 實際帶出 `last_activity_at`／`workers.cpu_active` 有效值，記錄門檻選值依據（心跳 60s → 門檻 ≥300s）
- [ ] 部署新腳本至 `/usr/local/sbin/ti-layer3-monitor.sh` 並確認 timer 正常輪轉（systemd 單元不改）
- [ ] 實測一輪長任務不誤殺：在真實 autopilot 長任務進行期間連續 ≥3 輪監控執行，取 journal 證據證明零誤報、零 restart
