## 任務 #1 完成：新增 `studio/discussion.py` 通用 DiscussionEngine：支援任意 N 個角色（ExpertLike 介面）、`round_robin`（依序）與 `parallel`（同輪並行、輪間同步，asyncio.gather＋複用 `_llm_semaphore()` 節流）兩種模式；context 餵法為「議題＋上一輪全員發言＋自己的歷史發言」而非全史重播；回傳結構化 transcript（輪次/角色/全文）

## 任務 #2 完成：實作互相回應與反諂媚機制：發言 prompt 要求 `回應 @角色名: 同意|反對 ＋理由` 結構化引用、且「至少指出一個可挑戰點，無異議須說明為何」；在 discussion.py 加 `parse_mentions()` 解析發言中的 @引用為結構化記錄（防禦式：格式不符整段視為無引用，不 silent 錯位）

## 任務 #3 完成：實作收斂控制與討論小結：最大輪數上限（`TI_DISCUSS_MAX_ROUNDS`）＋沿用 `flow.is_stalled()` 相似度自適應提前停止；討論結束輸出小結結構（共識清單/分歧清單/各角色最終立場），供後續結論彙整使用

## 任務 #4 完成：接線與設定：orchestrator 的 `_debate()` 在 `TI_DISCUSS_MODE=round_robin|parallel` 時改走 DiscussionEngine（未設或 `legacy` 時行為與現狀完全一致）；`config.py`/`settings.py` 白名單加新欄位、`.env.example` 補說明；更新 ARCHITECTURE.md 並在 discussion.py 模組 docstring 寫明**實際函式簽名**

## 任務 #5 完成：撰寫離線測試 `tests/core/test_discussion.py`：用 StubExpert 驗證依序/並行模式的發言順序與輪間同步、@引用解析（含格式不符退化案例）、最大輪數與 is_stalled 提前停止、`TI_DISCUSS_MODE` 未設時 `_debate` 走舊路徑

## 任務 #1 完成：建立角色設定檔載入器：新增 `studio/role_store.py`，定義角色檔格式為 `roles/*.md`（YAML frontmatter 放 key/name/avatar/title/model/allowed_tools/permission_mode/tags/description，body 即 system_prompt），用 pydantic 驗證後轉 frozen Role；啟動時以「內建 8 角色為預設、檔案同 key 覆蓋」合併進 `ROSTER`/`BY_KEY`，壞檔案明確拒絕（記 log、不影響內建角色），並附離線單元測試（檔案覆蓋內建／壞檔被拒／未知 frontmatter 欄位明確報錯）

## 任務 #2 完成：實作角色管理 API：在 routes.py 加 `GET/POST/PUT/DELETE /api/roles`（照既有 pydantic Body model＋auth 依賴慣例），寫入即落檔到 `roles/` 並 reload 角色表；內建角色可被覆蓋、不可刪除（刪除覆蓋檔＝還原內建）；建立/更新時驗證 system_prompt 非空且含出力格式段落（micro-rules，拒絕空殼 persona）；附 API 層測試

## 任務 #3 完成：實作討論小組（Group）：新增 `Group = {name, role_keys[], mode}` 概念與 `GET/POST/PUT/DELETE /api/groups`，存檔於 `roles/groups.yaml`（或同目錄設定檔）；組隊三條硬規則——role_key 必須存在、不得重複、≥2 人，違反即 4xx 明確報錯；mode 白名單沿用 `{round_robin, parallel}`，非法值報錯；附測試（含「引用不存在角色被拒」）

## 任務 #4 完成：同步文件與設定：把 #1~#3 的角色檔格式、API 欄位（request/response 每個欄位名與型別）寫入 ARCHITECTURE.md（或 docs/）；roles/ 目錄加一份範例角色檔；`.env.example` 與 settings.py 若有新增環境變數一併補上

## 任務 #5 完成：冒煙驗證：實際啟動 `python3 -m studio.server`，用 HTTP 走完「建角色→列出→編輯→組隊→刪除還原內建」全流程並核對回應；同時跑全測試套件確認既有測試零回歸（特別是依賴 `BY_KEY`/`ROSTER` 的 discussion/orchestrator 路徑）

