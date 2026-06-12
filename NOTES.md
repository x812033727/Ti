## 任務 #1 完成：新增 `studio/discussion.py` 通用 DiscussionEngine：支援任意 N 個角色（ExpertLike 介面）、`round_robin`（依序）與 `parallel`（同輪並行、輪間同步，asyncio.gather＋複用 `_llm_semaphore()` 節流）兩種模式；context 餵法為「議題＋上一輪全員發言＋自己的歷史發言」而非全史重播；回傳結構化 transcript（輪次/角色/全文）

## 任務 #2 完成：實作互相回應與反諂媚機制：發言 prompt 要求 `回應 @角色名: 同意|反對 ＋理由` 結構化引用、且「至少指出一個可挑戰點，無異議須說明為何」；在 discussion.py 加 `parse_mentions()` 解析發言中的 @引用為結構化記錄（防禦式：格式不符整段視為無引用，不 silent 錯位）

## 任務 #3 完成：實作收斂控制與討論小結：最大輪數上限（`TI_DISCUSS_MAX_ROUNDS`）＋沿用 `flow.is_stalled()` 相似度自適應提前停止；討論結束輸出小結結構（共識清單/分歧清單/各角色最終立場），供後續結論彙整使用

## 任務 #4 完成：接線與設定：orchestrator 的 `_debate()` 在 `TI_DISCUSS_MODE=round_robin|parallel` 時改走 DiscussionEngine（未設或 `legacy` 時行為與現狀完全一致）；`config.py`/`settings.py` 白名單加新欄位、`.env.example` 補說明；更新 ARCHITECTURE.md 並在 discussion.py 模組 docstring 寫明**實際函式簽名**

## 任務 #5 完成：撰寫離線測試 `tests/core/test_discussion.py`：用 StubExpert 驗證依序/並行模式的發言順序與輪間同步、@引用解析（含格式不符退化案例）、最大輪數與 is_stalled 提前停止、`TI_DISCUSS_MODE` 未設時 `_debate` 走舊路徑

