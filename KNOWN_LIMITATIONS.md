# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 產出選題依據報告：聚合 history meta 的 latency/token 統計證明全零、估算 `_COMMON`＋角色 prompt token 數確認超過快取最低門檻（Sonnet/Opus ≥1024），落檔 `docs/perf/prompt-cache-selection.md`
- [ ] 撰寫前後對比與補驗文件：離線證據（單測輸出）＋真 API 補驗指令（`GET /api/metrics` 比對 cache_read/creation），落檔 `docs/perf/prompt-cache-verification.md`
