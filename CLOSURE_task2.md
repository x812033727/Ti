# 任務 #2 關閉說明：ruff format 條件執行（受影響檔 → zero diff，不動工結案）

> 本文件取代先前（09:23 `530dc03`）誤植的「collection 綠燈」結案——那份屬前一輪
> collection 任務、貼的是 `pytest --collect-only` 輸出（2555），與本輪 ruff format 任務無關，
> 已於本輪覆蓋。本結案專就 **任務 #2：`ruff format`（不加 --check）** 立論。

## 任務定義回顧
- 觸發條件：**若 #1 判定需 reformat**，則僅對受影響檔執行 `ruff format`（不加 --check）；
  **若 #1 已 0 檔，則不動工、標記已完成**。

## 動工判定：task#1 名單為 10 檔，但當前 HEAD 重確認為 0 diff → 不動工

task #1 的 `BASELINE_task1.md` 是在 HEAD `6b9701a` 量測，列出 10 個「would reformat」的受影響檔。
**本輪在當前 HEAD `ab517e0`、以 CI 鎖定版 ruff 0.14.4 重新確認，這 10 檔已全部 zero diff。**

原因：當前 HEAD 相對 `origin/main` **無任何 `.py` 差異**（`git diff --name-only origin/main...HEAD | grep '\.py$'` 空），
那 10 檔的已格式化版本早已隨主幹/merge（merge-base `899a750`）落地。故 task#1 的 10 檔名單是過時快照，
當前已無格式債 → **依設計決策「條件性動工」，命中「0 檔不動工」分支，拒絕無謂 reformat（鍍金）。**

## 實測證據（全程 ruff 0.14.4，隔離 `.qa-venv`，未污染全域；repo root）

### A. 對 task#1 指名的 10 個受影響檔，實際執行 `ruff format`（不加 --check）
```
$ ruff format tests/autopilot/test_autopilot_synonym_dedup.py \
    tests/autopilot/test_qa_task3_failclosed_contract.py \
    tests/autopilot/test_qa_task4_dualpath_parity.py \
    tests/autopilot/test_qa_task4_subsystem_filter.py \
    tests/autopilot/test_qa_task5_escape_hatch.py \
    tests/core/test_claude_dedup_backlog_task4_qa.py \
    tests/core/test_claude_no_double_backoff_task3_qa.py \
    tests/core/test_providers_max_retries_task1_qa.py \
    tests/core/test_tools_idempotent_no_dedup_task6.py \
    tests/test_task1_retry_doc.py
10 files left unchanged          # exit 0
```
→ 即使**真的執行**格式化動作（非 --check 純查核），結果為 **`10 files left unchanged`**，
   執行後 `git diff` 對這 10 檔 **0 檔變動**。zero diff 由實際動作證明，非僅靠查核推斷。

### B. 全庫驗收：`ruff format --check .`
```
$ ruff format --check .
298 files already formatted       # exit 0
```

### C. 工作樹狀態
```
$ git diff --name-only origin/main...HEAD | grep '\.py$'    # 空：本分支零 .py 異動
$ git status --porcelain                                    # 空：工作樹乾淨
```

## 對應驗收標準
| 標準 | 實測 | 狀態 |
|------|------|------|
| 1. `ruff format --check studio/ tests/` exit 0（0 需 reformat）| 298 files already formatted，exit 0 | ✅ |
| 5. 空 diff ＋ 根因說明，不為湊改動而 reformat | 10 檔實跑 `ruff format` 仍 zero diff；空 diff 結案 | ✅ |

## 為何不執行 reformat（不是漏做，是正確結果）
- `ruff format`（不加 --check）已**實際對 10 檔執行**，輸出 `10 files left unchanged`、執行後 zero diff——
  證明無格式債可補，再 reformat 屬鍍金，依 YAGNI 拒絕。
- 與 doc-only 護欄一致：本分支相對 `origin/main` 不得動任何 `.py`（`test_no_py_changed`）；
  若強行製造 diff 反而會踩中該護欄翻紅。不動工同時滿足兩端。

## 移交待辦（範圍外，不阻擋本任務）
- `.gitmodules` 存取被權限拒（環境限制，非本任務）。
- `tests/conftest.py` Starlette/httpx deprecation warning（warning 非 error，不影響收集）。
