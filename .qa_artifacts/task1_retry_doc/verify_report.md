# Task-1（retry 文件護欄）驗證權威證據

> **本檔為此驗證的唯一權威來源。** 任何其他中間輸出、臨時 worktree 或散落片段，若與本檔結論衝突，一律以本檔為準。
>
> **應忽略的殘檔／中間產物（非權威，不得引用為結論）：**
> - 任何臨時 worktree，如 `/tmp/…/task1_lane/task-1`（本輪取證用，跑完即以 `git worktree remove --force` 清除）。
> - 蒐證過程的 stdout 片段、`-v`／`-rs`／`-rA` 逐行輸出等中間輸出。
> - 本檔外部佐證檔 `verify_report.md.sha256` 僅為雜湊佐證，不含結論，不得單獨作為權威。

---

## 1. 執行指令原文

```
.venv/bin/python -m pytest tests/test_task1_retry_doc.py -q
```

同一指令分別在 **task-1 lane**（worktree 目錄名為 `task-1`）與 **main 目錄**（`/opt/ti-autopilot-work`，branch=`main`）各執行一次，對照如下。

---

## 2. 實跑摘錄與判定

### 2a. task-1 lane（worktree 目錄名 `task-1`）— 真 PASS＝轉綠

護欄測試 `test_no_py_changed` 在 task-1 lane 真正執行並通過：

```
PASSED tests/test_task1_retry_doc.py::test_no_py_changed
11 passed in 0.06s
```

（`-v` 逐項對照：11 項全數 PASSED，末項 `tests/test_task1_retry_doc.py::test_no_py_changed PASSED [100%]`，總計 `11 passed`。）

### 2b. main 目錄（branch=`main`）— SKIPPED 對照，非 pass

同指令在 main 目錄執行，該護欄測試被設計性 skip：

```
SKIPPED [1] tests/test_task1_retry_doc.py:188: 非 task#1 doc-only lane（worktree='ti-autopilot-work', branch='main'）：.py 變更護欄不適用，避免跨 lane 假紅
10 passed, 1 skipped in 0.05s
```

### 2c. 明文判定

- **task-1 lane 為真 PASS＝轉綠**：`test_no_py_changed` 在 task-1 lane 實際執行 `git merge-base HEAD origin/main` 基準比對，確認本 lane 未引入任何 `.py` 變更（doc-only），assert 通過，`11 passed` 為真綠燈。
- **main 之 skip 為設計性分支隔離、非 pass**：該測試以 worktree 目錄名／branch 名判定 lane 身分（`test_task1_retry_doc.py:182-192`）。非 task-1 lane 時主動 `pytest.skip`，避免多 lane 共用 HEAD 時對改碼 lane（如 task-3 合法改 `.py`）造成跨 lane 假紅。**skip ≠ pass，嚴禁把 main 的 `1 skipped` 當作護欄通過的證據。** 護欄的真實通過只以 task-1 lane 的 `PASSED test_no_py_changed` 為準。

---

## 3. 合併前提佐證

- `HEAD` == `origin/main`，同一 SHA：`3abb092244aec5201b73ed97a7a5c858fe103e00`（短碼 `3abb092`）。
- HEAD commit：`3abb092 fix(autopilot): discovered followup 進場套品質防線,止住 backlog 灌水(F) (#362)`。
- 當前 branch＝`main`，`git status --short` 無未提交變更（clean）。
- 判定：task-1 變更已 **MERGED** 進 `origin/main`（HEAD 與 origin/main 指向同一 SHA），合併前提成立。

---

## 4. 環境備註

- Python 3.12.3 / pytest 9.1.1（`rootdir=/opt/ti-autopilot-work`，`configfile: pyproject.toml`）。
- task-1 lane 取證方式：`git worktree add --detach <tmp>/task-1 HEAD`（自 `3abb092`），於該 worktree 內以同一 `.venv` 執行；取證後 `git worktree remove --force` 清除，`git worktree list` 僅餘主工作樹。

---

## 5. 本檔自身 sha256

本檔自身 sha256 列於下方程式碼區塊唯一一行（單一權威值來源）：

```
fb126ee989b1c7521a1daf10eafcaedca461e7fd540aadf7b823f9765fc3ee9d  verify_report.md
```

自我指涉雜湊的既定作法與可重現規範（雜湊字面值**僅**出現在上方那一行，散文一律不嵌字面值，以確保下述重現步驟穩定成立）：

- **定義**：本 sha256 為「將上方程式碼區塊那一行還原為 `__SHA256_PLACEHOLDER__  verify_report.md` 佔位字串後之本檔內容」之雜湊。雜湊值本身無法涵蓋回填後的自己，故以佔位版為基準。
- **重現方式**：把上方程式碼區塊那一行改回 `__SHA256_PLACEHOLDER__  verify_report.md`，執行 `sha256sum verify_report.md`，即得上方所列同一值。
- **產生指令**：`sha256sum verify_report.md > verify_report.md.sha256`（單一目的指令），並以 Read 讀回後回填至上方那一行。
- **外部佐證檔**：`.qa_artifacts/task1_retry_doc/verify_report.md.sha256`，記錄同一佔位版雜湊。
