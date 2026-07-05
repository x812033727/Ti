# Prompt Cache A/B Report

- 真實 API：是（Claude Agent SDK 正式 `Expert.speak()` 路徑）
- model：`claude-sonnet-4-6`
- effort：`agent_sdk_default`
- role/system_prompt：`engineer` / sha256 `388ff10e64c924cffb75a1a073d48097e16c82deda04dccad4bb3e6726c88fb3`
- allowed_tools：`Read, Write, Edit, Bash, Grep, Glob`
- cwd：`/opt/ti-autopilot-work.lanes/lane-apd337e27107-3/.qa_artifacts/prompt_cache_ab/workdir`
- prompt sha256：`c1eef4e4a4296f1ac653ba622ac2187ed892b295a5840916d8a5054037721552`
- after 命中證據：PASS (`cache_read_input_tokens=28878`)

| 組別 | DISABLE_PROMPT_CACHING | ttft_s | cache_read_input_tokens | cache_creation_input_tokens | duration_ms | prompt_tokens | completion_tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| before | 1 | 2.661 | 0 | 0 | 3748 | 28881 | 11 |
| after | unset | 1.602 | 28878 | 0 | 2511 | 3 | 11 |

## Before/After 對比

- before ttft_s：`2.661`
- after ttft_s：`1.602`
- after - before：`-1.059` 秒
- before cache_read_input_tokens：`0`
- after cache_read_input_tokens：`28878`

註：`ttft_s` 是本專案串流包裝層量到的首個內容事件時間，適合看同路徑 A/B delta；絕對值不宣稱等同 provider 原生 TTFT。
