# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 覆蓋啟用門禁與空字串停用門禁的稽核訊息測試，並驗證不洩漏新舊密碼、hash、token、cookie
- [ ] 覆蓋寫入失敗時不發成功稽核訊息，且環境與 config 不被誤更新
- [ ] 執行 scoped lint、測試與 `git status --short`，確認無未追蹤或非本需求變更
