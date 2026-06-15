# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 執行 `git fetch origin` 並蒐集四條驗證命令的原始輸出與 exit code（status --porcelain=v2 --branch、diff --quiet origin/main HEAD、diff --quiet --cached、rev-parse HEAD vs origin/main）
- [ ] 確認並記錄假性 diff 排除政策（submodule、CRLF/eol、staged/untracked），說明本 repo 為何不受影響
- [ ] 撰寫驗證關閉說明文件，含標頭（branch/HEAD/origin-main/fetch 時間/執行者）、四條命令原始輸出、exit code，明確標示「空 diff」結論
- [ ] 由 PM/QA 對照關閉說明與實跑結果覆核，確認證據鏈完整且工作目錄無新殘留
