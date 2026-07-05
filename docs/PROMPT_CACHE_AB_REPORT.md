# Prompt Cache A/B Report

- 真實 API：否（run 完成但 `cost_usd<=0` 或無 `token_usage`，推測走 fallback／離線路徑；A/B 數字不視為真實 API 對比）
- 模式：`real`
- model：`claude-fable-5`
- effort：`agent_sdk_default`
- role/system_prompt：`engineer` / sha256 `388ff10e64c924cffb75a1a073d48097e16c82deda04dccad4bb3e6726c88fb3`
- allowed_tools：`Read, Write, Edit, Bash, Grep, Glob`
- cwd：`/opt/ti-autopilot-work.lanes/lane-apd337e27107-3/.qa_artifacts/prompt_cache_ab/workdir`
- prompt sha256：`c1eef4e4a4296f1ac653ba622ac2187ed892b295a5840916d8a5054037721552`
- after 命中證據：PASS (`cache_read_input_tokens=90`)

| 組別 | DISABLE_PROMPT_CACHING | ttft_s | cache_read_input_tokens | cache_creation_input_tokens | duration_ms | prompt_tokens | completion_tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| before | 1 | 0.123 | 0 | 0 | 1234 | 100 | 10 |
| after | unset | 0.123 | 0 | 90 | 1300 | 100 | 10 |
| after_read_2 | unset | 0.123 | 90 | 0 | 200 | 10 | 10 |

## Before/After 對比

- before ttft_s：`0.123`
- after ttft_s：`0.123`
- after - before：`0.000` 秒
- before cache_read_input_tokens：`0`
- after cache_read_input_tokens：`90`

註：`ttft_s` 是本專案串流包裝層量到的首個內容事件時間，適合看同路徑 A/B delta；絕對值不宣稱等同 provider 原生 TTFT。
