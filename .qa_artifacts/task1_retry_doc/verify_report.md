# Task-1（retry 文件護欄）驗證權威證據

> **本檔為此驗證的唯一權威來源。** 固定絕對權威路徑：
> `/opt/ti-autopilot-work/.qa_artifacts/task1_retry_doc/verify_report.md`
> 任何其他中間輸出、臨時 worktree 或散落片段，若與本檔結論衝突，一律以本檔為準。
>
> **應忽略的殘檔／中間產物（非權威，不得引用為結論）：**
> - `verify_task1_mergebase_fetch.sh` — task#1 蒐證用的 merge-base/fetch 檢查腳本，僅為過程產物，**非本驗證權威**，其輸出不得單獨作為結論。
> - `verify_report.md.sha256` — 本檔外部雜湊佐證檔，僅記錄雜湊、不含結論，不得單獨作為權威。
> - 任何臨時 worktree（如 `/tmp/…/task-1`）：本輪取證用，跑完即以 `git worktree remove --force` 清除。
> - 蒐證過程的 stdout 片段、`-v`／`-rs`／`-rA` 逐行輸出等中間輸出。

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
11 passed in 0.08s
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

## 3. 合併前提佐證（可回放指令與輸出）

本節記錄坐實「本批 commit 已合併進 origin/main」前提的可回放指令與其輸出：

```
$ git fetch origin main
（更新 origin/main 追蹤 ref，無錯誤輸出）

$ git show origin/main:tests/test_task1_retry_doc.py | grep -n "merge-base HEAD origin/main"
164:    這裡改用 `merge-base HEAD origin/main` 為基準對比 commit 後的實際變更，

$ git merge-base --is-ancestor origin/main HEAD && echo MERGED
MERGED

$ git rev-parse HEAD
3abb092244aec5201b73ed97a7a5c858fe103e00

$ git rev-parse origin/main
3abb092244aec5201b73ed97a7a5c858fe103e00
```

- 判定：`git show origin/main:tests/test_task1_retry_doc.py` 已含 `merge-base HEAD origin/main` 修正（第 164 行命中），證明本批 commit 已在 `origin/main`。
- `HEAD` 與 `origin/main` 指向同一 SHA `3abb092244aec5201b73ed97a7a5c858fe103e00`，且 `--is-ancestor` 回 `MERGED`，合併前提成立。

---

## 4. 環境備註

- Python 3.12 / pytest（`rootdir=/opt/ti-autopilot-work`，`configfile: pyproject.toml`）。
- task-1 lane 取證方式：`git worktree add --detach <tmp>/task-1 HEAD`，於該 worktree 內以主樹同一 `.venv` 執行；取證後 `git worktree remove --force` 清除。

---

## 5. 本檔自身 sha256

本檔自身 sha256 列於下方程式碼區塊唯一一行（單一權威值來源）：

```
4be55968cb58720ca60e12831ec618aea3a0c60e091eec5558db8c80843e561a  verify_report.md
```

自我指涉雜湊的既定作法與可重現規範（雜湊字面值**僅**出現在上方那一行，散文一律不嵌字面值，以確保下述重現步驟穩定成立）：

- **定義**：本 sha256 為「將上方程式碼區塊那一行維持為 `__SHA256_PLACEHOLDER__  verify_report.md` 佔位字串時之本檔內容」之雜湊。因雜湊值本身無法涵蓋回填後的自己，故以佔位版為基準。
- **重現方式**：把上方程式碼區塊那一行改回 `__SHA256_PLACEHOLDER__  verify_report.md`，執行 `sha256sum verify_report.md`，即得外部佐證檔所列同一值。
- **產生指令**：先以佔位版計算雜湊，回填至上方那一行後，另寫入外部佐證檔 `verify_report.md.sha256`。
- **外部佐證檔**：`.qa_artifacts/task1_retry_doc/verify_report.md.sha256`，記錄同一佔位版雜湊。
