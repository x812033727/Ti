# 任務 #1 基準快照：ruff / pytest --collect-only / pytest

- 本任務性質：**doc-only**（產出基準快照）。本分支相對 `origin/main` **不引入任何 `.py` 變更**，
  滿足既有護欄 `tests/test_task1_retry_doc.py::test_no_py_changed`。
- 量測 HEAD：`6b9701a`（撤回 10 個 .py 重排版後，working tree 相對 `origin/main` 的 `.py` diff = 0；
  待 orchestrator 提交本次撤回後，committed `base..HEAD` 的 `.py` diff 亦為 0）。
- 環境：本地 `ruff 0.15.12`（PATH）、CI 釘版 `ruff 0.14.4`、`pytest 9.0.3`、`python3`。

## 三命令結果（當前 HEAD 實況）

| 層級 | 命令 | 結果 | 狀態 |
|------|------|------|------|
| lint | `ruff check .` | exit 0「All checks passed!」 | ✓ |
| lint | `ruff format --check .` | **exit 1（10 檔 would reformat）** | ✗ **pre-existing** |
| collect | `python3 -m pytest --collect-only -q` | exit 0，**2566 collected** | ✓ |
| test | `python3 -m pytest -q` | **exit 1：1 failed（`test_ruff_format_check_dot_passes`），其餘全綠** | ✗ **pre-existing** |

> 完整 pytest 在當前 HEAD 的**唯一**失敗是 `tests/scan/test_scan_shell_usage_no_pollution.py::test_ruff_format_check_dot_passes`。
> 此失敗**非本任務引入**，詳見下節證據。

## 關鍵發現：「全綠」驗收在 doc-only 約束下不可達——主幹本身即紅

**證據（皆可複現）：**
1. `origin/main` tip == merge-base == `3863735`。在 `origin/main` 上，10 個測試 `.py` 檔本就**未經 `ruff format`**
   （以 CI 釘版 `ruff 0.14.4` 與本地 `0.15.12` 雙版檢查，兩版皆 `would reformat` 同 10 檔，**排除版本飄移**）。
2. `test_ruff_format_check_dot_passes` 從 PATH 呼叫 `ruff format --check .`，故在 `origin/main` 上**本身即紅**。
3. 受影響 10 檔：
   - tests/autopilot/test_autopilot_synonym_dedup.py
   - tests/autopilot/test_qa_task3_failclosed_contract.py
   - tests/autopilot/test_qa_task4_dualpath_parity.py
   - tests/autopilot/test_qa_task4_subsystem_filter.py
   - tests/autopilot/test_qa_task5_escape_hatch.py
   - tests/core/test_claude_dedup_backlog_task4_qa.py
   - tests/core/test_claude_no_double_backoff_task3_qa.py
   - tests/core/test_providers_max_retries_task1_qa.py
   - tests/core/test_tools_idempotent_no_dedup_task6.py
   - tests/test_task1_retry_doc.py

**矛盾本質（兩護欄互斥於 doc-only 分支）：**
- 若為了讓 `test_ruff_format_check_dot_passes` 轉綠而 `ruff format` 重排這 10 個 `.py` →
  觸發 `test_no_py_changed`（要求本分支相對 `origin/main` 不得動任何 `.py`）變紅。
- 若維持 doc-only（不動 `.py`）→ `test_ruff_format_check_dot_passes` 維持與 `origin/main` 相同的紅。
- 故「`ruff check .` 與**完整** `pytest` 在當前 HEAD 全綠」在 doc-only 約束下**本質不可達**，
  根因是**主幹自身的 pre-existing 失敗**，非本快照任務可在範圍內修復。

> 第 1 輪曾誤把 10 個 `.py` 重排版綁進 commit（試圖讓 `format --check` 轉綠），反而踩中
> `test_no_py_changed`。**第 2 輪已撤回**，回歸 doc-only。

## 移交 PM 的決策事項（超出本任務範圍）
- pre-existing 失敗 `test_ruff_format_check_dot_passes`（`origin/main` 即紅）須**另開獨立任務**處理，二擇一：
  - (A) 對 10 檔執行 `ruff format`（需在允許動 `.py` 的任務內，避開 `test_no_py_changed`）；或
  - (B) 調整/修正該 format 護欄測試本身。
- 在該獨立任務完成前，後續 #2/#3/#4 的「完整 pytest 全綠」驗收應理解為
  「**除既有 `test_ruff_format_check_dot_passes` 外無新增失敗**」。

## 「已拆 / 未拆」現況盤點

| 區塊 | 現況 | 對應任務 |
|------|------|----------|
| CI `lint` job | `Ruff lint`(`ruff check .`) 與 `Ruff format check`(`ruff format --check .`) **已是各自獨立 step** ✓ | 無需改動 |
| CI `test` job | 僅單一 `Run tests` step（`python -m pytest -q --cov=studio ...`），**collect 與 run 混在同一 step** ✗ | **#2 缺口** |
| CI `sandbox-test` job | step 內部已用 `--collect-only` 動態取選中數，屬該 step 私有邏輯、非獨立步驟；**#4 守護測試須只鎖 `jobs['test']`，勿全檔掃 `--collect-only`（否則被 sandbox job 假綠）** | #4 注意事項 |
| orchestrator 三閘門 | `_gate_lint` / `_gate_collect_without_sdk` / `_gate_tests` **已分離、各有 note**，但回報缺 `[lint]`/`[collect]`/`[test]` 層級標籤 ✗ | **#3 缺口** |

## 審查意見處置紀錄
- 【高工問題 1（pathspec）】高工稱 `git diff ... -- '*.py'` 的 `*` 不跨 `/`、子目錄漏掃——**經實測為誤**：
  該測試確實掃到 `tests/autopilot/*`、`tests/core/*` 等子目錄 `.py`（git pathspec 無 `:(glob)` magic 時 `*` 會跨 `/`）。
  失敗訊息已列出全部 10 個子目錄檔可證。故**不採納**「pathspec 漏掃」之跟進待辦（前提不成立）。
- 【高工問題 2】已補「量測 HEAD：6b9701a」。
- 【高工問題 3 / 資安】helper 腳本 log 改用 `mktemp -d` 暫存、結束 `trap` 清除，消除固定檔名併發互蓋。

## 自測
- 指令：`bash scripts/baseline_selftest.sh`（wall-time ~43s，<60s）。
- 退出 0 語意：lint 綠、collect 綠（2566）、doc-only 不變式成立（相對 `origin/main` 無 `.py` 變更）、
  且完整 pytest **僅剩**唯一 pre-existing 失敗 `test_ruff_format_check_dot_passes`、無任何回歸。
- 腳本以 commit 無關的 `git diff origin/main -- '*.py'` 驗 doc-only（避免 `test_no_py_changed` 因
  「撤回尚未提交」的時序假紅），並扣除該測試後跑完整 pytest 斷言失敗集合。
- 等價單進程指令（wall-time ~67s）：
  `ruff check . ; echo "[lint exit=$?]" ; python3 -m pytest --collect-only -q ; echo "[collect exit=$?]" ; python3 -m pytest -q ; echo "[test exit=$?]"`

## 結論
- lint 層在 CI 已拆妥；orchestrator 三閘門邏輯已分離。兩個真實缺口：CI `test` job 的 collect step（#2）、orchestrator 回報標籤（#3）。
- 本任務回歸 doc-only：**未引入任何 `.py` 變更、無回歸**。
- 唯一 pytest 失敗為主幹既有的 `test_ruff_format_check_dot_passes`，已如實記錄並移交 PM 另案處理。
