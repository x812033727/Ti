# 已知限制（Known Limitations）

目前本輪交付項目已完成，無殘留的交付阻塞限制。

## 已關閉項目

- [x] `config.py` 三處同步（頂層宣告、`reload()` global、`reload()` 賦值）新增 `NOTIFY_WEBHOOK`（`TI_NOTIFY_WEBHOOK`，預設空字串）與 `NOTIFY_TIMEOUT`（`TI_NOTIFY_TIMEOUT`，預設 10 秒）兩鍵。
- [x] 更新 prompt 規則與文件：教 PM 輸出 `禁改:` 行，並載明 marker 格式與比對語意。
