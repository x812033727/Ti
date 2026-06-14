# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 在 OpenAIExpert.speak() 將 _chat 工具迴圈包進 attempt_fn 並接 run_with_retries(**make_retry_config().as_kwargs())，掛 on_api_error/on_rate_limit_exhausted 回退為空字串

## RetryConfig 統一退避入口（task #5 收斂）

- [ ] **`base/cap/jitter` 建構後 mutate 不影響已生成 backoff**：未加 `frozen=True`（避免 `object.__setattr__` 繞凍降低可讀性），故屬性技術上仍可被外部寫入，但 `__post_init__` 已把 clamp 後的本地值固化進閉包——建構後改屬性「看似生效、實則無效」。目前僅靠 docstring 警語守邊界，無執行期防護。後續若要硬化，評估改 `frozen=True` 並以 `object.__setattr__` 寫回。
- [ ] **`jitter` 預設 0.0（確定值）**：為向後相容刻意保留（既有測試對確定延遲有預期），未採 AWS 建議的 0.25 主動防 thundering herd。生產若需抗驚群，須由呼叫端（或 `EXPERT_RATE_LIMIT_BACKOFF_JITTER` config）顯式調高，預設不主動開啟。
- [ ] **欄位快照 vs lazy backoff 的潛在分歧**：`make_retry_config` 的欄位值（建構時 config 快照）與 `_backoff_delay`（retry 當下 lazy-read）同源同鍵、常態一致；但若 retry 進行中 config 被改寫，兩者會短暫分歧（欄位顯示舊值、實際退避用新值）。屬刻意取捨（lazy-read 語意優先），非缺陷，惟需知悉。
