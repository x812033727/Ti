# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 對照需求逐項比對 `publish-release.yml`／`scripts/publish_release.py`／`release-smoke.yml`，產出「已達成／缺口」清單
- [ ] 確認 `GH_PAT` 設定指引齊備、發佈文件納入 DoD，並明文標註「真實 tag-push 端到端尚待生產驗證」
- [ ] 將任務 #1 找到的任何殘留死角收口到核心 `llm_caller`，呼叫端只保留注入 config/callback 的薄包裝
- [ ] 補/確認 guard 測試，鎖住「退避秒數計算與錯誤文字分類器唯一實作在 `llm_caller`」，任一呼叫端複製手寫即變紅
