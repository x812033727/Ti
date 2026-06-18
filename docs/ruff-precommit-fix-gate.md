# Ruff pre-commit 自動修正閘驗收紀錄

- 日期：2026-06-18
- 目標：確認現有 `.pre-commit-config.yaml` 的 `ruff` hook（`args: [--fix]`）在自動修正檔案後，是否會讓 pre-commit 非零退出。
- 結論：現況已滿足「修檔後不可靜默通過」，不需要修改 hook。

## 實測

建立最小 probe 檔：

```python
import os

print("ok")
```

執行：

```bash
git add qa_ruff_probe.py
.venv/bin/python -m pre_commit run ruff --files qa_ruff_probe.py
git diff -- qa_ruff_probe.py
```

結果：

```text
exit_code=1
ruff (legacy alias)......................................................Failed
- hook id: ruff
- files were modified by this hook

Found 1 error (1 fixed, 0 remaining).
```

修正 diff：

```diff
diff --git a/qa_ruff_probe.py b/qa_ruff_probe.py
index 4f87b9a..817c3bb 100644
--- a/qa_ruff_probe.py
+++ b/qa_ruff_probe.py
@@ -1,3 +1,2 @@
-import os
 
 print("ok")
```

## 決策

- 保留 `.pre-commit-config.yaml` 的 `args: [--fix]`。
- 不新增 wrapper script、不新增第二套掃描器、不新增永久端到端測試。
- 開發者遇到 Ruff 自動修正時，提交會停止；重新 stage 後再 commit。
