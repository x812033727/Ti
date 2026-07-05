# 退避 jitter 收口盤點（任務 #1）

> 目的：逐一標出 **orchestrator 與 experts 串流路徑**上所有可能產生「退避延遲」的呼叫點，
> 證明它們**皆匯流至唯一入口** `make_retry_config` → `backoff_delay`，且 jitter 實際值
> ＝`config.EXPERT_RATE_LIMIT_BACKOFF_JITTER`（預設 **0.5**），**無第二條繞過 jitter 的退避**。
>
> 本盤點是**文件產物**（檔名:行號＋狀態），不引入程式依賴。行號以本輪 repo 實際值為準。
> 統計去同步的白/黑樣本測試見任務 #2/#3；SDK 不疊乘見任務 #4；DECISIONS 記錄見任務 #5。

## 結論（一句話）

orchestrator **不自建任何退避**——所有 LLM 發言一律經 `expert.speak()`；兩條 provider 串流路徑
（Claude／OpenAI 相容）都在發言層取 `make_retry_config()` 後交 `llm_caller.run_with_retries`，
而 `run_with_retries` 內**唯一**的退避秒數計算點是注入的 `backoff` callback（源頭＝
`backoff_delay(jitter=0.5)`），**唯一**的等待點是注入的 `sleep`。無旁路。

## 唯一退避入口的證據鏈

| 步 | 位置 | 事實 |
| --- | --- | --- |
| 1 | `studio/config.py:443` | `EXPERT_RATE_LIMIT_BACKOFF_JITTER = _env_float("TI_RATELIMIT_BACKOFF_JITTER", 0.5)`——jitter 的 SSOT，消費端 default-on＝**0.5**。（`config.py:1203` 於 `reload()` 同鍵複寫，執行期一致。） |
| 2 | `studio/experts.py:109`-`135` | `make_retry_config()` 建 `RetryConfig`：`jitter=config.EXPERT_RATE_LIMIT_BACKOFF_JITTER`（`experts.py:132`）、顯式注入 `backoff=_backoff_delay`（`experts.py:133`）、`sleep=_sleep`（`experts.py:134`）。這是 experts 層退避策略的**單一真實來源**。 |
| 3 | `studio/experts.py:86`-`97` | `_backoff_delay` 薄包裝：呼叫時（retry 當下）委派 `llm_caller.backoff_delay(retry_after, attempt, base=…, cap=…, jitter=config.EXPERT_RATE_LIMIT_BACKOFF_JITTER)`（`experts.py:96`）——jitter 於 retry 當下 lazy-read config，恆＝0.5。 |
| 4 | `studio/llm_caller.py:412`-`448` | `backoff_delay` 為**唯一**退避公式：429（有 retry-after）走向上 jitter（`:443` `min(nominal*(1+j*rand()), cap)`）；529／無 retry-after 走 equal-jitter 向下散（`:448` `nominal*(1-j*rand())`）；`j==0` 兩路徑 early-return 確定值（`:441`/`:446`）。rand 注入縫在 `:419`/`:436`。 |
| 5 | `studio/llm_caller.py:578`-`589` | `RetryConfig.as_kwargs()` 只平鋪 `max_retries`／`backoff`／`sleep` 三個 config 驅動參數給 `run_with_retries`——即步 2 注入的 `_backoff_delay` 原封傳遞，不另生成。 |
| 6 | `studio/llm_caller.py:657` | retry 迴圈內**唯一**的退避秒數計算：`delay = backoff(ra, attempt)`（`backoff` 即步 2 注入者）。**唯一**等待點：`studio/llm_caller.py:674` `await sleep(delay)`。無其他 delay/sleep 分支。 |

## 呼叫端盤點（orchestrator / experts 串流路徑）

| # | 呼叫點（檔名:行號） | 角色 | 狀態 | 判定依據 |
| --- | --- | --- | --- | --- |
| C1 | `studio/experts.py:565` `_speak_with_retries()` | Claude 串流發言主體 | ✅ 收口 | `experts.py:578` `cfg = make_retry_config()` → `experts.py:651` `run_with_retries(**cfg.as_kwargs(), …)`。整段 `start()+query()+stream_to_events()` 打包為 `_attempt`（`experts.py:580`-`605`）由骨幹重試，退避一律走 cfg。 |
| C2 | `studio/experts.py:365`-`378` `_build_client()` | Claude SDK client 建構 | ✅ 天然單層（無旋鈕） | `ClaudeAgentOptions` **不暴露** `max_retries` 旋鈕（`experts.py:373`-`378` 註解），Claude 路徑天然單層退避，無從也無需另設 0；client 層**禁**再加任何 retry/backoff，否則與 C1 疊乘。 |
| O1 | `studio/providers.py:884` `OpenAIExpert.speak()` | OpenAI 相容（openai/minimax/gemini）串流發言主體 | ✅ 收口 | `providers.py:902` `cfg = make_retry_config()` → `providers.py:1066` `run_with_retries(**cfg.as_kwargs(), …)`。與 C1 共用同一 `make_retry_config()` 旋鈕、同一 `backoff_delay(jitter=0.5)`。 |
| O2 | `studio/providers.py:1160`-`1171` `_openai_chat()` | OpenAI SDK client 建構 | ✅ 已解除疊乘 | `providers.py:1167` `AsyncOpenAI(..., max_retries=0)`——顯式讓位給 `run_with_retries`，SDK 內建重試（預設 2）已關，**不**與外層退避疊乘。 |
| Orc1 | `studio/orchestrator.py` 全部 `.speak()` 呼叫（如 `:446`/`:566`/`:731`/`:932`/`:983`/`:1202`/`:1287`/`:2192`/`:2282`/`:3528`/`:3567` …） | 各階段/任務/審查發言 | ✅ 委派 | orchestrator 內**無**任何 `run_with_retries`／`backoff`／`asyncio.sleep`／`time.sleep`（全檔 grep 為零）；一律透過 `expert.speak()` 落到 C1 或 O1。 |
| Orc2 | `studio/orchestrator.py:22` `from .experts import … make_retry_config` | re-export | ℹ️ 僅轉出，非第二入口 | orchestrator 匯入 `make_retry_config`／`make_retry_observer` 供 `_speak` 掛可觀測接點，**不**另建 `RetryConfig`；實際退避仍在 C1/O1 的發言層執行。 |
| Orc3 | `studio/discussion.py:315`-`318`（orchestrator 經 `DiscussionEngine` 消費串流） | 多角色討論發言 | ✅ 委派 | 只包 semaphore 後 `expert.speak()`，無本地退避，落到 C1/O1。 |

## 反證：無第二條繞過 jitter 的退避

- **串流三檔零裸 sleep**：`experts.py`／`providers.py`／`orchestrator.py` 內**無** `asyncio.sleep`／
  `time.sleep`（全 repo grep）。`llm_caller.py` 內僅兩處：`:514`（`_default_sleep` body，即 cfg.sleep 的實作）
  與 `:674`（retry 迴圈唯一等待點）——兩者皆在唯一退避骨幹內，非旁路。
- **`experts.py:106` `_sleep`** 委派 `llm_caller._default_sleep`，僅作 monkeypatch 接點，傳入 `run_with_retries`，不獨立等待。
- **範圍外（非旁路，刻意不共用核心）**：`studio/publisher.py:299`-`301` `_backoff`（用於 `:647`）屬 GitHub PR 合併／CI
  輪詢重試，無 retry-after 語意、無 SSE/provider 錯誤分類、cap 60s 為輪詢節奏而非 LLM token 節流——與 LLM 串流退避無關，
  不在本盤點範圍（詳見 `studio/docs/llm_caller_inventory.md` 的「範圍外」節）。

## 既有護欄（交叉佐證，非本任務新增）

- `tests/test_task3_wiring_acceptance_qa.py`：防 consumer 端手寫 `backoff_delay` lambda、限制 `RetryConfig` 建構點。
- `tests/core/test_wiring_retry_config.py`：證 OpenAI `speak()`／`complete_once()` 只進一層 `run_with_retries`，參數來自 `make_retry_config()`。
- `tests/core/test_retry_convergence_task5_qa.py`：禁 `complete_once` 第二層 retry、禁 `providers.py` 裸 sleep 退避。
- `tests/core/test_providers_max_retries_task1_qa.py`：OpenAI/minimax client 建構必須 `max_retries=0`（含反向黑樣本）。
