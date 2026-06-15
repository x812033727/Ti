# 任務 #1 基準快照：ruff / pytest --collect-only / pytest

HEAD: `a2901bd`（量測時工作樹乾淨）
量測環境：本地 `ruff 0.15.12`（PATH）、CI 釘版 `ruff 0.14.4`、`pytest 9.0.3`、`python3.x`

## 三命令結果

| 層級 | 命令 | as-found（HEAD 原狀） | 修正後（綠基準） |
|------|------|----------------------|------------------|
| lint | `ruff check .` | exit 0 ✓ | exit 0 ✓ |
| collect | `python -m pytest --collect-only -q` | exit 0 ✓（2566 collected） | exit 0 ✓（2566 collected） |
| test | `python -m pytest -q` | **exit 1 ✗（1 failed, 2557 passed, 8 skipped）** | exit 0 ✓（2558 passed, 8 skipped） |

執行指令（單進程，等價但 wall-time ~67s）：
`ruff check . ; echo "[lint exit=$?]" ; python3 -m pytest --collect-only -q ; echo "[collect exit=$?]" ; python3 -m pytest -q ; echo "[test exit=$?]"`

快速自測指令（測試切兩組平行、wall-time ~45s，結果等價：合計 2558 passed / 8 skipped）：
`ruff check . ; echo "[lint exit=$?]" ; python3 -m pytest --collect-only -q ; echo "[collect exit=$?]" ; bash scripts/run_tests_parallel.sh ; echo "[test exit=$?]"`
> `scripts/run_tests_parallel.sh` 僅以 shell 背景程序把 `tests/` 切兩組平行跑、合併 exit code，**未新增任何依賴**（不用 pytest-xdist）；純為縮短自測 wall-time，CI 仍走原單一 pytest。

## as-found 的唯一失敗：既有 format 缺漏（非本任務引入、非版本飄移）

- 失敗測試：`tests/scan/test_scan_shell_usage_no_pollution.py::test_ruff_format_check_dot_passes`
- 根因：HEAD 上有 **10 個 tests/ 檔未經 `ruff format`**，`ruff format --check .` 回 exit 1。
- **排除版本飄移**：以 CI 釘版 `ruff 0.14.4` 與本地 `0.15.12` 分別 `format --check`，**兩版皆指向同 10 檔**，故非「CI 用新版」造成的飄移，是真實未排版。
- 受影響 10 檔：
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

### 處置
以 CI 釘版 `ruff 0.14.4` 執行 `ruff format .`（純排版、零邏輯改動），10 檔重排版。
複驗：`0.14.4` 與 `0.15.12` 兩版 `format --check .` 皆綠，完整 pytest 轉綠（2558 passed, 8 skipped）。
此舉滿足整體驗收「`ruff check .` 與完整 `pytest` 在當前 HEAD 全綠」之前提，後續 #2/#3/#4 得以對綠基準驗收。

## 「已拆 / 未拆」現況盤點

| 區塊 | 現況 | 對應任務 |
|------|------|----------|
| CI `lint` job | `Ruff lint`(`ruff check .`) 與 `Ruff format check`(`ruff format --check .`) **已是各自獨立 step** ✓ | 無需改動 |
| CI `test` job | 僅單一 `Run tests` step（`python -m pytest -q --cov=studio ...`），**collect 與 run 混在同一 step**，collection error(exit 2) 與測試失敗(exit 1) 糊在一起 ✗ | **#2 缺口** |
| CI `sandbox-test` job | step 內部已用 `--collect-only` 動態取選中數，但屬該 step 私有邏輯、非獨立步驟；**#4 守護測試須只鎖 `jobs['test']`，勿全檔掃 `--collect-only`（否則被 sandbox job 假綠）** | #4 注意事項 |
| orchestrator 三閘門 | `_gate_lint` / `_gate_collect_without_sdk` / `_gate_tests` **已分離、各有 note**，但回報缺 `[lint]`/`[collect]`/`[test]` 層級標籤 ✗ | **#3 缺口** |

## 結論
- lint 層在 CI 已拆妥；orchestrator 三閘門邏輯已分離。
- 兩個真實缺口：CI `test` job 的 collect step（#2）、orchestrator 回報標籤（#3）。
- 既有 format 缺漏已修，綠基準建立，#2/#3/#4 可在此基準上驗收。
