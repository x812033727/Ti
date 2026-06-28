# 任務 #4 關閉說明：離線 E2E 與失敗待辦優先序驗證

## 結論

任務 #4 是驗證型任務；本輪修正點不是生產碼，而是把交付的 demo/驗收指令補到真的涵蓋交付物：

- 離線 E2E：`tests/test_offline_e2e.py`
- 失敗萃取與升格：`tests/core/test_orchestrator.py`
- P0 失敗待辦經真實 backlog 優先於 P1 建議：`test_failure_followups_sort_before_retro_items_in_backlog`
- 既有相關回歸：`test_backlog.py`、`test_improvement_plan.py`、`test_core_change_routing.py`

## 修正後執行指令

```bash
python3 -m pytest -q tests/core/test_orchestrator.py tests/test_offline_e2e.py tests/core/test_backlog.py tests/core/test_improvement_plan.py tests/core/test_core_change_routing.py
```

## 覆蓋理由

原三檔指令會綠，但沒有跑到離線 E2E，也沒有跑到 `test_failure_followups_sort_before_retro_items_in_backlog`，因此無法證明「失敗萃取 → 回填 → `next_pending` 優先取出」整鏈。

修正後指令直接包含 `tests/core/test_orchestrator.py`，其中排序測試使用真實 `backlog.add_items` 與 `backlog.next_pending`，不靠 mock；同時包含 `tests/test_offline_e2e.py`，符合任務標題的離線 E2E 要求。

## 全測補充

全套測試若遇到 `test_false_diff_exclusion_policy_evidence` 的 `.gitmodules` `PermissionError`，屬本 lane `.gitmodules` 是字元裝置造成的環境問題，與本鏈的 `flow/orchestrator/backlog` 無關。

**執行指令: `python3 -m pytest -q tests/core/test_orchestrator.py tests/test_offline_e2e.py tests/core/test_backlog.py tests/core/test_improvement_plan.py tests/core/test_core_change_routing.py`**
