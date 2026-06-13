# 冒煙驗證報告（SMOKE_REPORT.md）

> 由 `scripts/smoke_llm.py --report` 自動產出。本報告如實記錄抽樣與量測結果，不粉飾；數值對應本次執行的 transcript，可回溯。

- **本次是否用真實 API**：離線 stub（`--offline`）
- 涵蓋模式：`round_robin`、`parallel`

## 一、發言品質抽樣 ＆ 二、@引用遵循率（按模式）

### mode = `round_robin`（rounds=2, concurrency=2，僅 parallel 生效）

- 發言總數：8；stop_reason：`max_rounds`

**發言品質抽樣**（首行＋解析出的合法 Mention 數；顯示前 6/8 筆，已截斷）：

| 輪 | 發言者 | mentions | 首行摘要 |
| --- | --- | --- | --- |
| R1 | 專案經理 | 0 | 專案經理 開場意見：先界定問題邊界，我認為最該優先處理的是流程骨架在限流/錯誤下的韌性。 |
| R1 | 工程師 | 1 | 工程師 第 1 次發言，補一個可挑戰點：先驗證再放行。 |
| R1 | 高級工程師 | 1 | 高級工程師 第 1 次發言，補一個可挑戰點：先驗證再放行。 |
| R1 | 研究員 | 1 | 研究員 第 1 次發言，補一個可挑戰點：先驗證再放行。 |
| R2 | 專案經理 | 1 | 專案經理 第 2 次發言，補一個可挑戰點：先驗證再放行。 |
| R2 | 工程師 | 1 | 工程師 第 2 次發言，補一個可挑戰點：先驗證再放行。 |

**@引用格式遵循率**（複用 `discussion.parse_mentions` 解析結果，非另寫一套）：

- 整體遵循率：**100.0%**（7/7 應回應發言；已排除 1 筆結構上不可能引用的開場發言）
- 合法 Mention 總數：7
  - R1：100.0%（3/3，mentions=3）
  - R2：100.0%（4/4，mentions=4）

**共識判定**（區分「全員無反對」與「強共識」，不誤判）：

- 分類：**全員無反對＋有明確同意（強共識）**
- `no_dissent=True` `has_explicit_agreement=True` `is_strong_consensus=True`（同意 7 條／反對 0 條）

### mode = `parallel`（rounds=2, concurrency=2）

- 發言總數：8；stop_reason：`max_rounds`

**發言品質抽樣**（首行＋解析出的合法 Mention 數；顯示前 6/8 筆，已截斷）：

| 輪 | 發言者 | mentions | 首行摘要 |
| --- | --- | --- | --- |
| R1 | 專案經理 | 0 | 專案經理 開場意見：先界定問題邊界，我認為最該優先處理的是流程骨架在限流/錯誤下的韌性。 |
| R1 | 工程師 | 0 | 工程師 開場意見：先界定問題邊界，我認為最該優先處理的是流程骨架在限流/錯誤下的韌性。 |
| R1 | 高級工程師 | 0 | 高級工程師 開場意見：先界定問題邊界，我認為最該優先處理的是流程骨架在限流/錯誤下的韌性。 |
| R1 | 研究員 | 0 | 研究員 開場意見：先界定問題邊界，我認為最該優先處理的是流程骨架在限流/錯誤下的韌性。 |
| R2 | 專案經理 | 1 | 專案經理 第 2 次發言，補一個可挑戰點：先驗證再放行。 |
| R2 | 工程師 | 1 | 工程師 第 2 次發言，補一個可挑戰點：先驗證再放行。 |

**@引用格式遵循率**（複用 `discussion.parse_mentions` 解析結果，非另寫一套）：

- 整體遵循率：**100.0%**（4/4 應回應發言；已排除 4 筆結構上不可能引用的開場發言）
- 合法 Mention 總數：4
  - R2：100.0%（4/4，mentions=4）

**共識判定**（區分「全員無反對」與「強共識」，不誤判）：

- 分類：**全員無反對＋有明確同意（強共識）**
- `no_dissent=True` `has_explicit_agreement=True` `is_strong_consensus=True`（同意 4 條／反對 0 條）

## 三、rate limit（429）行為

- **離線未觸發**：`--offline` 注入 `StubExpert`，發言不經 `studio/experts.py` 真實 API 路徑，全程不對 `api.anthropic.com` 發請求，故**未觸發任何 429**。
- 「在第幾併發撞 429」：**N/A（離線未觸發）**。各 mode 併發設定如下，僅供真實面回歸時對照：
  - `round_robin`：concurrency=2（序列發言、併發旗標不生效）
  - `parallel`：concurrency=2（asyncio.Semaphore）

## 四、SDK 錯誤文字命中數

（指 SDK 把 API 錯誤塞進 `AssistantMessage` 文字、被 `experts.py` 偵測為該輪失敗走 fallback 的筆數；與 429 為兩條獨立 counter，不混計。）

- **離線未觸發**：stub 不產生 SDK 錯誤文字，命中數恆為 **0**。
  - `round_robin`：0 筆
  - `parallel`：0 筆

## 五、誠實標註：未涵蓋真實 API 面（移交待辦）

本次為**離線 stub 跑**，**未涵蓋真實 API 面**，以下為明示移交待辦：

- 真實 429 行為、實際撞限流的併發臨界點，**本次未驗**——本 sandbox 網路白名單不含 `api.anthropic.com`，真實請求打不通。
- SDK 把錯誤塞進 `AssistantMessage` 文字的真實樣態，**本次未驗**（防線已有單元測試覆蓋，但非真實鏈路）。
- 補驗方式：在具 ANTHROPIC 金鑰＋外網的環境，去掉 `--offline` 重跑`--mode round_robin` 與 `--mode parallel`（建議併發由小漸增），再以本腳本`--report` 產出真實面報告比對。
