# 任務 #1 交付：autopilot 每日 token 預算熔斷

## 結論

本輪不是 doc-only，也不是零 `.py` 變更。

在 PM 未補明確 AC 的情況下，採用既有 `TI_AUTOPILOT_DAILY_PR_BUDGET` 的同層模式，落地最小可驗收增量：新增 `TI_AUTOPILOT_DAILY_TOKEN_BUDGET`，讓 autopilot 在 UTC 當日 token 用量達上限後停止取新任務與自評，睡到跨日後自動恢復。

## 實作範圍

- 修改：`.env.example`，新增 `TI_AUTOPILOT_DAILY_TOKEN_BUDGET` 範例。
- 修改：`studio/config.py`，新增設定值與 `reload()` 支援。
- 修改：`studio/autopilot.py`，新增每日 token 統計與預算 gate，並讓 self-eval 寫入 history 以納入統計。
- 新增：`tests/autopilot/test_daily_token_budget.py`，覆蓋 token budget 的核心行為。
- 修正：`tests/test_task1_retry_doc.py`，避免上一輪 doc-only 護欄誤判本輪實作需求；改為驗證本輪 `.py` 異動範圍。
- 更新：`tests/qa_task1_blocker_truthfulness.py`，把 QA 驗證改成檢查本文件與 git 事實一致。

## 驗收條目

- `TI_AUTOPILOT_DAILY_TOKEN_BUDGET=0` 時不限制，維持既有行為。
- 只統計 UTC 當日、`requirement` 以 `[autopilot]` 開頭的 history meta。
- 壞 token 資料與非 autopilot 場次會跳過，不讓 gate 誤判。
- 達 token 上限時，主迴圈先睡眠，不進 clone、self-eval、取任務或跑任務。
- self-eval 的 token usage 會寫入 history，後續可被預算統計吃到。

## 驗證指令

```bash
python3 -m pytest tests/autopilot/test_daily_token_budget.py tests/test_task1_retry_doc.py::test_no_py_changed tests/qa_task1_blocker_truthfulness.py -q
python3 -m ruff check studio/autopilot.py studio/config.py tests/autopilot/test_daily_token_budget.py tests/test_task1_retry_doc.py tests/qa_task1_blocker_truthfulness.py
python3 -m pytest --collect-only -q
```
