# Prompt Cache 前後對比與補驗

## 結論（先講範圍與限制）

- 本輪改善＝在 `studio/experts.py` 的 `ClaudeAgentOptions` 依旋鈕 `TI_PROMPT_CACHE_1H`（預設開）傳入
  `env={"ENABLE_PROMPT_CACHING_1H": "1"}`，解鎖 Claude Agent SDK subprocess 的 prompt caching。
- **未打真 API、快取命中證據目前 N/A**。離線只能證明「開啟態 options 確實帶了 env、關閉態不帶」，
  **無法**證明 Claude CLI（Node 層）是否認得該 env、也無法證明實際命中率。命中閉環須待真實 session 後，
  以 cache token 欄位觀測補齊（見下方補驗指令）。

## 前後對比（行為差異）

| 面向 | Before（本輪之前） | After（本輪之後） |
|------|------|------|
| `ClaudeAgentOptions.env` | 未設定（走 SDK default `{}`），從不傳 `ENABLE_PROMPT_CACHING_1H` | 旋鈕開時帶 `env={"ENABLE_PROMPT_CACHING_1H": "1"}`；關閉時不傳 key（仍為 `{}`，非 `None`） |
| 旋鈕 | 無 | `TI_PROMPT_CACHE_1H`（`config.PROMPT_CACHE_1H`，預設開），`config.py` 頂層與 `reload()` 兩處同步 |
| 既有 history 的 `cache_read`/`cache_write` | 全為 0（見 `prompt-cache-selection.md` 快照） | 待真 session 後觀測 `cache_read` 是否 > 0 |

實作要點（helper 對稱、避免 `env=None` 地雷）：

```python
# studio/experts.py
def _prompt_cache_options() -> dict:
    if not config.PROMPT_CACHE_1H:
        return {}
    return {"env": {"ENABLE_PROMPT_CACHING_1H": "1"}}
```

SDK `env` 合併語意為 **merge**（`{**os.environ, ...cli_defaults, **options.env}`，實查
`subprocess_cli.py`）：單鍵 dict 安全傳入，`ANTHROPIC_API_KEY` 等 parent env 不受影響。關閉態刻意
**不傳** `env` key（而非 `env=None`）——SDK 在 `**self._options.env` 展開時對 `None` 會 `TypeError`。

## 離線證據（單元測試）

離線可自證的邊界：options 帶 env 兩態行為，由 `tests/core/test_prompt_cache.py` 覆蓋。

重跑指令（禁裸 `python`/`pytest`，一律走 venv 模組）：

```bash
timeout 300 .venv/bin/python -m pytest -q tests/core/test_prompt_cache.py
```

實跑輸出（本 lane，2026-07-11）：

```
....                                                                     [100%]
4 passed in 0.83s
```

四條測試對應的斷言：

- `test_prompt_cache_options_enabled`：開啟態 `_prompt_cache_options() == {"env": {"ENABLE_PROMPT_CACHING_1H": "1"}}`。
- `test_build_client_passes_prompt_cache_env_when_enabled`：`_build_client(...)` 造出的 client
  其 `options.env == {"ENABLE_PROMPT_CACHING_1H": "1"}`。
- `test_build_client_omits_env_when_prompt_cache_disabled`：關閉態 options **不含** `env` key，
  且 `env` 非 `None`（守住 `env=None` 的 `TypeError` 地雷）。
- `test_config_reload_picks_up_prompt_cache_env`：`TI_PROMPT_CACHE_1H=0/1` 經 `config.reload()`
  正確反映到 `config.PROMPT_CACHE_1H`。

**這些測試證明的是「傳遞正確」，不是「快取命中」。** 命中與否取決於 CLI Node 層與 API，離線不可驗。

## 真 API 補驗指令（命中閉環）

前置：設好 `ANTHROPIC_API_KEY`、旋鈕維持預設開（或顯式 `TI_PROMPT_CACHE_1H=1`）。

1. 背景啟動服務（勿前景常駐；`PID=$!` 記下行程號，收尾用它精準 kill，避免誤殺其他行程）：

   ```bash
   nohup .venv/bin/python -m studio.server > /tmp/ti-server.log 2>&1 &
   PID=$!
   sleep 3 && curl -sf http://localhost:8000/api/health
   ```

2. 用真 API 跑一場 session（經 web `/ws` 送需求，或走既有 E2E 流程），讓多個角色至少各發言兩輪，
   使同一 session 內第 2 次起可命中同前綴的 `_COMMON`＋角色 prompt。

3. **運維總覽**：查 `GET /api/metrics`，比對 cache 相關欄位（SDK 回報的
   `cache_read_input_tokens` / `cache_creation_input_tokens`，在 Ti 內落為 `cache_read` / `cache_write`）
   的比例。此端點給跨場運維視角（sessions / history / scorecard 等）：

   ```bash
   # 運維總覽：跨場 metrics（含各場 token_usage 的 cache 欄位比對入口）
   curl -sf http://localhost:8000/api/metrics | .venv/bin/python -m json.tool
   ```

4. **逐場 cache 明細**：cache 欄位由 `history._derive_token_usage` 聚合到**每場 session meta 的
   `token_usage`**（`cache_read`＝命中量、對應 `cache_read_input_tokens`；`cache_write`＝寫入量、對應
   `cache_creation_input_tokens`）。取單場明細：

   ```bash
   # 單場：查該 session meta 的 token_usage cache 欄位
   curl -sf "http://localhost:8000/api/history/<SESSION_ID>/events?limit=1" \
     | .venv/bin/python -m json.tool | grep -A8 '"token_usage"'
   ```

   判讀：`token_usage.total.cache_read > 0` 即代表快取命中；命中率＝
   `cache_read / (prompt + cache_read + cache_write)`（與 `studio/usage_report.py::_cache_hit_pct` 同式，
   該函式回傳的是 `*100` 的百分比）。before（未帶 env）預期 `cache_read == 0`；after 開啟且 CLI 認得 env
   時預期 `cache_read` 上升、對應角色的 `latency.by_role.avg_ms` 應同步下降。

5. 驗畢收掉服務並清掉可能含機敏 env 的 log：

   ```bash
   kill "$PID" 2>/dev/null
   rm -f /tmp/ti-server.log
   ```

## 已知邊界

- **CLI Node 層是否實際認得 `ENABLE_PROMPT_CACHING_1H`**：不可從 Python SDK 原始碼驗證，屬已知邊界。
  本文件「未打真 API」聲明已涵蓋；正式上線後以真 session 的 `cache_read` token 觀測補閉環。
- **cache token 的兩層觀測**：`/api/metrics` 給跨場運維總覽；cache token 的逐場明細由
  `token_usage` 聚合，經 `/api/history` 取得對應場次的 `cache_read` / `cache_write`。兩者搭配即可完成
  before/after 比對，無需新增埋點。
- **本 clone 無真數據**：既有 200 場 history 的 `cache_read`/`cache_write` 全為 0（`prompt-cache-selection.md`
  快照），只是「快取路徑從未啟用」的空基準，不是命中證據。
