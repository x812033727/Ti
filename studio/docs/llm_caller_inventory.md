# LLM 呼叫端收口盤點（2026-06-21）

## 行號守門

- 類型：`line-number`
- 狀態：`planned`
- 守門測試：`tests/docs/test_inventory_line_guard_llm_caller.py`
- 模板：`studio/docs/inventory_line_guard_convention.md`
- 原則：文件只作被校驗方；測試須由實碼動態重算行號，不得為了過測試改產品碼或新增 wrapper。

範圍：`studio/` 內實際會發起 LLM 發言或轉接串流的路徑，重點檢查 429/529 退避、SSE/SDK 錯誤文字分類、provider 不可用文字防線是否委派 `studio.llm_caller`。

## 結論

- Claude Agent SDK 路徑已收口：`experts.Expert.speak()` 包住 `query()+stream`，統一進 `llm_caller.run_with_retries`。
- OpenAI 相容路徑（openai/minimax/gemini）已收口：`OpenAIExpert.speak()` 統一進 `llm_caller.run_with_retries`，SDK `max_retries=0`。
- orchestrator / discussion 串流層沒有自建 LLM stream/retry；只透過 `expert.speak()` 委派 provider。
- #2 已收口死角 2 個：Codex JSONL error 零 exit 路徑已先進核心不可用分類；Antigravity auth phrase 已移除 provider 端冗餘白名單，改全委派核心 pattern。

## 核心唯一實作

| 能力 | 狀態 | 回指 |
| --- | --- | --- |
| 公開契約與消費端定位 | 已在核心 | `studio/llm_caller.py:1`-`18` |
| SSE error type -> HTTP status 對照，含 429/529 | 已在核心 | `studio/llm_caller.py:47`-`68`, `studio/llm_caller.py:141`-`159` |
| 錯誤文字分類，避免寬鬆關鍵字 | 已在核心 | `studio/llm_caller.py:32`-`45`, `studio/llm_caller.py:162`-`190` |
| provider 不可用文字分類 | 已在核心 | `studio/llm_caller.py:273`-`304` |
| 429 retry-after / 529 指數退避 | 已在核心 | `studio/llm_caller.py:383`-`419` |
| retry 骨幹與 callback 注入 | 已在核心 | `studio/llm_caller.py:563`-`668` |

## 呼叫端狀態

| 呼叫端 | 狀態 | 判定 |
| --- | --- | --- |
| `experts.Expert` / Claude SDK | 已委派 | `experts.py:76`-`85` 將分類/退避委派核心；`experts.py:383`-`390` 只把串流文字命中轉成核心 signal 子類；`experts.py:452`-`548` 整段 `query()+stream_to_events()` 進 `llm_caller.run_with_retries`。 |
| Claude SDK client | 已避免雙層 retry | `experts.py:305`-`310` 明確說 ClaudeAgentOptions 無 `max_retries`，不可再加 client 層 retry。 |
| `providers.OpenAIExpert`（openai/minimax/gemini） | 已委派 | `providers.py:825`-`838` 宣告整個工具迴圈交核心；`providers.py:991`-`999` 實際呼叫 `llm_caller.run_with_retries`。 |
| OpenAI 相容 SDK 建構 | 已避免雙層 retry | `providers.py:1043`-`1047` 建 `AsyncOpenAI(..., max_retries=0)`。 |
| `providers.complete_once` | 已委派到 `speak()`，不自套第二層 | `providers.py:1087`-`1110` 說明退避在 `speak()`；`providers.py:1132`-`1137` 實際只 `make_expert(...).speak(...)` 並吞最終兜底。 |
| `providers.CodexExpert` | 已委派 | 非零 exit 與 JSONL `error`/`turn.failed` 零 exit 都經 `_codex_pause_or_soft()` 委派 `llm_caller.provider_unavailable_kind()` 判型；provider 端只決定 hard pause 或本輪 soft note。 |
| `providers.AntigravityExpert` | 已委派 | `_antigravity_unavailable()` 僅委派 `llm_caller.provider_unavailable_reason()`；`_antigravity_pause_or_soft()` 只依核心 kind 決定 transient/pause，無本地 auth phrase 白名單。 |
| `orchestrator.StudioSession._speak` | 已委派 / 不適用 | `orchestrator.py:1151`-`1161` 只做 semaphore、task tag、provider-unavailable 穿透，實際 LLM 呼叫是 `ctx.experts[role_key].speak()`。 |
| orchestrator 透過 `_speak()` 的任務/審查路徑 | 已委派 | 合併衝突修復 `orchestrator.py:1558`-`1565`、工程師實作 `orchestrator.py:1650`、自我精修 `orchestrator.py:1683`、QA/高工/資安審查 `orchestrator.py:1717`-`1743`、huddle legacy `orchestrator.py:1940` 都只進 `_speak()`，沒有本地 retry/backoff。 |
| orchestrator 直接 `.speak()` 階段路徑 | 已委派 | 需求澄清 `orchestrator.py:291`-`300`、異議關卡 `_critic_gate` `orchestrator.py:411`-`417`、legacy 辯論/ADR `orchestrator.py:544`-`568`、DiscussionEngine ADR 蒸餾 `orchestrator.py:603`-`609`、逐子題 ADR `orchestrator.py:745`-`750`、架構師決策 `orchestrator.py:796`-`821`、調研/拆解 `orchestrator.py:946`-`980`、devops 整合驗證 `orchestrator.py:1074`-`1078`、最終驗收/檢討 `orchestrator.py:2065`-`2096`、CI 修正 `orchestrator.py:2257`-`2262` 都只呼叫 provider `speak()`。 |
| orchestrator 透過 `DiscussionEngine` 的串流消費端 | 已委派 | `_debate_via_engine` 建 engine 後 `run()`：`orchestrator.py:585`-`593`；逐子題討論：`orchestrator.py:716`-`724`；huddle round_robin/parallel：`orchestrator.py:1922`-`1930`。實際發言集中在 `discussion.py:315`-`318`，只包 semaphore 後呼叫 `expert.speak()`。 |
| orchestrator / reflexion `complete_once()` 路徑 | 已委派 | `_store_reflection()` 建 `_llm` callback 走 `providers.complete_once()`：`orchestrator.py:1983`-`1986`，再注入 `reflexion.reflect_and_store()`：`orchestrator.py:1988`-`1990`；`reflexion.py:87`-`90` 只呼叫注入的 `llm` 並 fallback，不自套退避。 |
| conclusion / lessons / autopilot / improver 延伸消費端 | 已委派 | `conclusion.py:253`、`lessons.py:257`-`263`、`autopilot.py:716`、`improver.py:333`-`348`/`475`-`477` 都透過 `speak()` 或 `complete_once()`，沒有本地退避骨幹。 |

## 範圍外：刻意不共用核心的退避（非死角）

`llm_caller` 的退避/分類專責 **LLM 呼叫**（429/529、SSE/SDK 錯誤文字、provider 不可用）。
`studio/` 另有一處退避屬**不同領域、刻意不共用核心**，列此避免被「單一來源」稽核或 guard 誤判為重複：

| 位置 | 領域 | 為何不委派核心 |
| --- | --- | --- |
| `publisher._backoff` (`studio/publisher.py:299`-`301`，用於 `:647`) | GitHub PR 合併 / CI 輪詢重試（409 race、5xx、網路） | 與 LLM 串流退避無關：無 retry-after 語意、無 SSE/provider 錯誤分類、cap 60s 為 GitHub 輪詢節奏而非 LLM token 節流。強行共用核心會把兩個無關的重試節奏耦合在一起。 |

因此任何「退避秒數計算只能在 `llm_caller` 一份」的 guard 必須把 `publisher._backoff` 列為正當例外；
否則加上即紅、逼出脆弱白名單（對應已退役的 backlog #233/#235/#236）。錯誤文字指紋（`rate limit`/`529`/`overloaded`）亦會與 `*_usage.py` 的**額度查詢**模組碰撞，後者是查 provider rate-limit 配額、與「分類 LLM 錯誤文字」無關。

## 死角清單（#2 已收口）

| 編號 | 檔案行號 | 類型 | 影響 | 建議 #2 收口 |
| --- | --- | --- | --- | --- |
| D1 | `studio/providers.py` | Codex JSONL `error`/`turn.failed` 零 exit 未委派核心分類 | 已修正：回系統 note 前先進 `llm_caller.provider_unavailable_kind()`；hard unavailable 升 `ProviderUnavailable("codex", detail)`，rate/overload/timeout/server 暫態維持本輪 soft note。 | 已收口，測試：`test_codex_jsonl_error_zero_exit_uses_core_unavailable_classification`、`test_codex_jsonl_rate_limit_zero_exit_is_soft_note`。 |
| D2 | `studio/providers.py`, `studio/llm_caller.py` | Antigravity auth phrase 冗餘本地白名單 | 已修正：provider 端移除本地 phrase 掃描，`_antigravity_unavailable()` 僅委派核心；核心測試涵蓋 `authorization code`。 | 已收口，測試：`test_antigravity_unavailable_delegates_to_llm_caller`、`test_provider_unavailable_reason_detects_provider_failures`。 |

## 已有護欄

- `tests/test_task3_wiring_acceptance_qa.py:143`-`188`：防 consumer 端手寫 `backoff_delay` lambda，並限制 `RetryConfig` 建構點。
- `tests/core/test_wiring_retry_config.py:103`-`172`：證明 OpenAI `speak()` 與 `complete_once()` 只進一層 `run_with_retries`，參數來自 `make_retry_config()`。
- `tests/core/test_retry_convergence_task5_qa.py:103`-`124`：禁止 `complete_once` 第二層 retry、禁止 `providers.py` 裸 sleep 退避。
- `tests/core/test_providers_max_retries_task1_qa.py:67`-`111`：OpenAI/minimax client 建構必須 `max_retries=0`，且有反向黑樣本。

核心改動: #2 已把 D1/D2 收口到 `studio.llm_caller`；呼叫端只保留 provider-specific pause/soft callback 決策。
