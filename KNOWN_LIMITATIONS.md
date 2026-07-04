# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 撰寫黑白樣本單元測試：白樣本斷言壓縮後 `qa_passed`/`senior_approved`/`parse_mentions`/`parse_core_changes` 解析結果與原文一致；黑樣本斷言「吃掉裁決行的破壞版」必被攔下
- [ ] 跑離線 e2e（`TI_OFFLINE` 假專家全流程）＋全測試＋ruff 驗證回歸，並在 `ARCHITECTURE.md` 補一段壓縮注入的敘述
