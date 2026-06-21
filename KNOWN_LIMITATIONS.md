# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 對照需求逐項比對 `publish-release.yml`／`scripts/publish_release.py`／`release-smoke.yml`，產出「已達成／缺口」清單
- [x] 確認 `GH_PAT` 設定指引齊備、發佈文件納入 DoD —— 真實 tag-push 端到端已於 2026-06-21 生產驗證：`v0.2.0` release 由 `GH_PAT`（Fine-grained, Contents:RW）建立並觸發 `release-smoke` 全綠，解除「`GITHUB_TOKEN` 建的 release 不觸發下游」死結。首次真實發佈同時暴露並修復 `publish-release.yml`／`release-smoke.yml` 兩處 import `studio` 卻未裝 `python-dotenv` 的潛伏崩潰（PR #232／#233）
- [ ] 將任務 #1 找到的任何殘留死角收口到核心 `llm_caller`，呼叫端只保留注入 config/callback 的薄包裝
- [ ] 補/確認 guard 測試，鎖住「退避秒數計算與錯誤文字分類器唯一實作在 `llm_caller`」，任一呼叫端複製手寫即變紅
