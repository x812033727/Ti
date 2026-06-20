# LLM 核心穩定公開契約

本頁是 Ti 核心 LLM 韌性中介層的穩定契約。實作以 `studio/llm_caller.py` 為準；本文件只描述呼叫端可依賴的公開面與維護邊界，不複製易漂移的公式細節。

## 適用範圍

- Claude 專家層、OpenAI 相容 provider 層，以及未來若需要直接接串流的 orchestrator 層，都必須透過 `llm_caller` 收斂退避、錯誤分類與觀測事件。
- 呼叫端只負責建立一次 `attempt_fn`（一次 query 加串流）與 fallback callback；不得各自手寫 429/529 分類器、退避秒數計算或第二層 retry。
- `experts.make_retry_config()` 是目前消費層取得 retry 旋鈕的唯一工廠，回傳 `RetryConfig`，再用 `cfg.as_kwargs()` 平鋪給 `run_with_retries`。

## 穩定公開介面

下游只應依賴這些名字；新增或破壞相容性都算核心契約變更：

- 訊號例外：`RateLimitSignal`、`OverloadedSignal`、`APIErrorSignal`、`ProviderUnavailable`。
- 分類入口：`classify_api_text(text)`、`classify_sse_error(error_type, message, partial_text=...)`、`classify_failure(exc)`、`sse_error_status(error_type)`、`is_rate_limit_type(error_type)`、`is_overloaded_type(error_type)`、`parse_retry_after(text)`。
- Provider 不可用判斷：`provider_unavailable_kind(text)`、`provider_unavailable_reason(text)`。
- 退避與重試：`RetryConfig`、`RetryConfig.as_kwargs()`、`backoff_delay(retry_after, attempt, *, base, cap, jitter, rand=None)`、`run_with_retries(...)`。
- 觀測介面：`RetryMetrics`、`Observer`、事件常數 `EV_RETRY`、`EV_RATE_LIMIT_EXHAUSTED`、`EV_API_ERROR`、`EV_TIMEOUT`、`EV_SUCCESS`、`EV_UNKNOWN_ERROR`。

標準接法：

```python
cfg = make_retry_config()
result = await llm_caller.run_with_retries(
    attempt_fn,
    **cfg.as_kwargs(),
    on_rate_limit_exhausted=handle_429_exhausted,
    on_api_error=handle_api_error,
    on_retry=observe_retry,
)
```

## 429 vs 529 退避策略

- 分類以結構化訊號優先：SDK typed exception、本層訊號例外、SSE `event:error` 的 `error.type`、JSON `error.type`。禁止用寬鬆關鍵字掃描正常模型文字，避免把「我遇過 rate limit」這類發言誤殺。
- 429 代表 `rate_limit_error` 或明確狀態碼 429。若有 `Retry-After`，`run_with_retries` 會把該秒數傳給 `backoff`，以伺服器要求為主；沒有時才走指數退避。耗盡後走 `on_rate_limit_exhausted`。
- 529 代表 `overloaded_error` 或明確狀態碼 529。529 不信任也不使用 `Retry-After`，`run_with_retries` 強制以 `backoff(None, attempt)` 走純指數退避；耗盡後走 `on_api_error`。
- Anthropic SSE 200-error 坑要用型別修正：串流中途若收到 `rate_limit_error` 或 `overloaded_error`，即使 SDK 例外的 `status_code` 是 200，也要依 `error.type` 分流成 429 或 529。
- 400/401/403/413、billing/quota/auth 等非可恢復錯誤直接 fallback 或標為 provider unavailable；未知例外原樣 re-raise，不用 retry 掩蓋程式錯。

## SDK `max_retries` 關閉約定

核心 `run_with_retries` 是唯一 retry 擁有者。任何 SDK 內建 retry 都必須關閉或明確證明不會與核心退避疊乘。

- OpenAI 相容路徑必須用 `openai.AsyncOpenAI(..., max_retries=0)` 建構；MiniMax/Gemini 共用同一路徑，所以一次套用。
- Claude Agent SDK 目前不注入 `retry`、`retries`、`backoff`、`max_retries` 等 SDK retry 旋鈕；若未來 SDK 新增預設 retry，接入時要顯式關閉。
- 新 provider 接入時，測試必須能證明 429/529 是單層退避：呼叫次數應為 `1 + max_retries`，不允許 SDK 預設 2 次再乘上核心重試。

## CORE_REPO 路由

本契約屬 Ti 核心韌性，不是單一產品專案邏輯。凡是修改公開介面、退避策略、SDK retry 關閉規則、SSE 錯誤分流或本文，都要走核心 repo：

- 路由目標是 `config.CORE_REPO`；它固定等於 `config.AUTOPILOT_REPO`，預設 `x812033727/Ti`。
- 團隊討論若發現專案需求需要改 Ti 核心，輸出結構化行：`核心改動: [P0/bug] <一句具體描述>`。
- `flow.parse_core_changes` 解析後，由 `backlog.route_core_changes(items)` 寫入核心 backlog；不得混入專案 backlog 或專案 PR。
- autopilot 在 `CORE_REPO` 的 working clone 上實作、測試，並對核心 repo 開獨立 PR。

## 維護檢查

改動本契約或相關實作時，至少重跑：

```bash
python3 -m pytest tests/test_llm_caller.py tests/test_llm_caller_observability.py tests/test_experts_ratelimit.py tests/core/ -q
```

文件一致性可重跑：

```bash
python3 -m pytest tests/test_llm_caller_public_contract_doc.py -q
```
