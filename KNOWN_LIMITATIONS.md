# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 撰寫輪替工作單 `docs/evidence/token-rotation-2026-07-10.md`：把 runbook 三步驟展開為可勾選清單（順序鎖死先發後撤）、逐步標人工/AI、留驗證輸出貼證欄位
- [ ] 驗證收尾：跑 `tests/server/` 全量＋`ruff check .`，貼跑綠輸出自證，`git status tests/` 確認無範圍外殘跡
