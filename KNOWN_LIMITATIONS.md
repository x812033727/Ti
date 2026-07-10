# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 實作已落盤憑證 scrub：下次 git 操作前偵測 `.git/config` remote URL 含自產 token pattern 並以 `set-url` 改寫（不 remove、寧漏勿誤刪）；legacy 閥開啟時跳過 scrub 的專屬黑白測試
