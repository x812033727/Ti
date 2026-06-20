# LLM 呼叫端收口盤點（2026-06-21）

範圍：`studio/` 內實際會發起 LLM 發言或轉接串流的路徑，重點檢查 429/529 退避、SSE/SDK 錯誤文字分類、provider 不可用文字防線是否委派 `studio.llm_caller`。

## 結論

- Claude Agent SDK 路徑已收口：`experts.Expert.speak()` 包住 `query()+stream`，統一進 `llm_caller.run_with_retries`。
- OpenAI 相容路徑（openai/minimax/gemini）已收口：`OpenAIExpert.speak()` 統一進 `llm_caller.run_with_retries`，SDK `max_retries=0`。
- orchestrator / discussion 串流層沒有自建 LLM stream/retry；只透過 `expert.speak()` 委派 provider。
- 仍需 #2 收口的死角有 2 個：Codex JSONL error 零 exit 路徑未進核心不可用分類；Antigravity auth phrase 仍在 provider 本地白名單。

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
| `providers.CodexExpert` | 部分委派，仍有死角 | `providers.py:365`-`368` 非零 exit 的 usage limit 透過 `_codex_usage_limited()`；`providers.py:441`-`443` 委派 `llm_caller.provider_unavailable_reason()`。但 `providers.py:271`-`272` 收到 JSONL `error`/`turn.failed` 後，若 `proc.returncode == 0`，`providers.py:373`-`377` 只回系統 note，未進核心分類。 |
| `providers.AntigravityExpert` | 部分委派，仍有死角 | `providers.py:481`-`484` 先委派 `llm_caller.provider_unavailable_reason()`；`providers.py:523`-`526` 用核心 kind 判斷 transient/pause。但 `providers.py:485`-`496` 仍有本地 auth phrase 白名單。 |
| `orchestrator.StudioSession._speak` | 已委派 / 不適用 | `orchestrator.py:1151`-`1161` 只做 semaphore、task tag、provider-unavailable 穿透，實際 LLM 呼叫是 `ctx.experts[role_key].speak()`。 |
| `discussion.DiscussionEngine` | 已委派 / 不適用 | `discussion.py:315`-`318` 只包 semaphore 後呼叫 `expert.speak()`。 |
| orchestrator 直接 `.speak()` call sites | 已委派 | 需求澄清 `orchestrator.py:291`-`300`、調研/拆解 `orchestrator.py:946`-`980`、架構討論 `orchestrator.py:544`-`568`/`796`-`821`、驗證審查 `_speak()` `orchestrator.py:1650`/`1717`-`1743`、收尾 `orchestrator.py:2065`-`2096` 都只透過 provider `speak()`。 |
| conclusion / lessons / autopilot / improver 延伸消費端 | 已委派 | `conclusion.py:253`、`lessons.py:257`-`263`、`autopilot.py:716`、`improver.py:333`-`348`/`475`-`477` 都透過 `speak()` 或 `complete_once()`，沒有本地退避骨幹。 |

## 死角清單

| 編號 | 檔案行號 | 類型 | 影響 | 建議 #2 收口 |
| --- | --- | --- | --- | --- |
| D1 | `studio/providers.py:271`-`272`, `studio/providers.py:373`-`377` | Codex JSONL `error`/`turn.failed` 零 exit 未委派核心分類 | Codex 若以 JSONL error 回報 rate/usage/auth，但 exit code 為 0，會被當一般系統 note，不會升 `ProviderUnavailable`，也不會走核心 rate/overload 分流。 | 在回系統 note 前，將 `"\n".join(errors)` 交 `llm_caller.provider_unavailable_reason/kind`；命中硬不可用則 raise `ProviderUnavailable("codex", detail)`，transient 則維持本輪 soft note。 |
| D2 | `studio/providers.py:485`-`496` | Antigravity auth phrase 本地白名單 | provider 不可用文字分類有第二份實作，未集中於 `llm_caller`。雖然不是 429/529 退避，但屬「錯誤文字防線」死角。 | 把這些 auth phrase 移到 `llm_caller` 的 provider-unavailable pattern，`_antigravity_unavailable()` 僅保留委派。 |

## 已有護欄

- `tests/test_task3_wiring_acceptance_qa.py:143`-`188`：防 consumer 端手寫 `backoff_delay` lambda，並限制 `RetryConfig` 建構點。
- `tests/core/test_wiring_retry_config.py:103`-`172`：證明 OpenAI `speak()` 與 `complete_once()` 只進一層 `run_with_retries`，參數來自 `make_retry_config()`。
- `tests/core/test_retry_convergence_task5_qa.py:103`-`124`：禁止 `complete_once` 第二層 retry、禁止 `providers.py` 裸 sleep 退避。
- `tests/core/test_providers_max_retries_task1_qa.py:67`-`111`：OpenAI/minimax client 建構必須 `max_retries=0`，且有反向黑樣本。

核心改動: 本輪只新增盤點文件，未修改 runtime；D1/D2 是後續 #2 的實作入口。
