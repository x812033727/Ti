# 結案紀錄：provider pre-flight routing 黑樣本驗收

本輪需求：修復後覆跑 `tests/core/test_provider_preflight_routing_qa.py` 兩個失敗黑樣本至全綠。

## 完成判定：完成（純驗證結案，零生產碼改動）

主鏈在現 main 已落地（git log 顯示為任務#4 成果），本輪定位為「驗收結案 + 假綠排除」，
不重做、不鍍金、不新增任何生產碼。

## 三證（比對現況）

- 生產護欄已落地：`studio/orchestrator.py`
  - `_pick_provider`（line 1503）對使用者明示覆寫角色第一行 early-return，連 pending marker 都不設。
  - `_explicit_provider_overrides`（line 1399）以 `config.is_user_explicit_provider` 過濾，空字串不進 overrides。
- 兩黑樣本綠且打在真實護欄上：
  - `test_pick_provider_explicit_override_wins_under_all_constrained`
  - `test_explicit_override_under_all_constrained_emits_no_event_or_audit`
- 破壞實驗還原後工作樹無殘留；本輪唯一落地物是本結案檔。

## 假綠排除（破壞 → 轉紅 → 還原 → 轉綠）

暫時移除 `_pick_provider` 的 `if config.is_user_explicit_provider(role.key): return ...` early-return：
- `test_pick_provider_explicit_override_wins_under_all_constrained` 轉 FAIL
  （`prov` 回 `claude` 而非 `codex`，並誤設 pending marker）→ 證明黑樣本有真判別力。
- `git checkout -- studio/orchestrator.py` 還原 → 2 黑樣本 PASS，除本結案檔外無其他工作樹殘留。

## 無回歸

收斂後執行指令（剔除環境敏感的離線 e2e，避免沙箱 read-only `lessons.lock` 紅燈混入）：

```
TI_SANDBOX=0 .venv/bin/python -m pytest \
  tests/core/test_provider_quota_helpers.py \
  tests/settings/test_provider_quota.py \
  tests/test_user_explicit_provider_contract.py \
  tests/core/test_provider_preflight_routing_qa.py \
  tests/autopilot/test_provider_routing_contract.py \
  tests/core/test_provider_all_constrained.py -q
```

結果：56 passed。

## 一句檢討（回寫教訓）

是否需回寫教訓：需。

教訓: 驗收的「pytest 全綠」務必指定不含環境敏感項的測試子集；環境性紅燈（例如 read-only 磁碟或網路限制）與被驗收碼無關，須在任務拆解時明確排除。
