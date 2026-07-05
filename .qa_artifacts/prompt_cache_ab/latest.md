# Prompt Cache A/B Report

- 真實 API：未完成
- 失敗原因：`ProviderUnavailable` You're out of usage credits. Run /usage-credits to keep using Fable 5 or /model to switch models.
- 補驗所需（任一即可）：
  - `ANTHROPIC_API_KEY` 環境變數已設定（API key 模式），或
  - 已登入的 `claude` CLI（`claude auth login` 訂閱模式；目前腳本環境需走 key）
- 補驗指令：
  ```bash
  # 1) 確認 Anthropic 憑證（API key 模式）
  test -n "$ANTHROPIC_API_KEY" && echo OK || echo MISSING
  # 2) 重跑 A/B 量測（單輪 timeout 60s，整體避免逾時）
  timeout 90 .venv/bin/python scripts/measure_prompt_cache_ab.py \
      --after-attempts 2 --turn-timeout 30
  ```
