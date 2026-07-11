# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 撰寫延遲數據實查報告（docs/latency-data-audit.md）：含可重現查核指令、全零證據（meta count 全 0、0 筆 token_usage）、選題依據改為研究證據的明文宣告
- [ ] 在 studio/roles.py 的 `_COMMON` 加入輸出長度上限指示（一般敘述回覆限 500 字內、結構化 marker 行豁免），並補守門測試斷言 8 個內建角色組合後 prompt 皆含此指示
- [ ] 產出前後對比報告（docs/latency-improvement-report.md）：離線層（指示注入 diff、8 角色覆蓋證據）＋真環境層明標「行為生效待補驗」並附具體補驗指令（跑一場真 session 後讀 meta latency by_role 與 output tokens 對比）
