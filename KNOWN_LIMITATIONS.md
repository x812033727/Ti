# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目用來追蹤已知限制與後續改良:

- [x] 在 config.py 三處同步（頂層宣告、reload() global、reload() 賦值）新增 `NOTIFY_WEBHOOK`（`TI_NOTIFY_WEBHOOK`，預設空字串）與 `NOTIFY_TIMEOUT`（`TI_NOTIFY_TIMEOUT`，預設 10 秒）兩鍵
- [x] 更新 prompt 規則與文件：教 PM 輸出 `禁改:` 行、載明 marker 格式與比對語意
