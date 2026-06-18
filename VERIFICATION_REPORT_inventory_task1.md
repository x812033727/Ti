# INVENTORY_task1.md 最小可解鎖證據包

日期：2026-06-18

執行時 HEAD：`86dca9abb1a40d4181d01dc9a09525c231419196`

本報告採用卡關 huddle 決議：#1 拆成硬門檻與軟證據。硬門檻只驗 `INVENTORY_task1.md` 是已追蹤檔、對 `HEAD` 無目標檔差異，且能追到真正引入 commit；`origin/main` 對齊與引用盤點只列風險，不阻塞 #2。

## 硬門檻結果

結論：通過，可作為後續文件屬性判定的最低證據。

```text
$ git status --short

```

採證時工作樹乾淨；套用本報告修正後，`git status --short` 可能顯示本報告自身修改，不能解讀成 `INVENTORY_task1.md` 有變更。

```text
$ git diff HEAD -- INVENTORY_task1.md

```

`INVENTORY_task1.md` 對 `HEAD` 無差異。

```text
$ git ls-files --stage INVENTORY_task1.md
100644 b0d7082eaea5885e35b7e23b8ce80fd14c016a77 0	INVENTORY_task1.md
```

`INVENTORY_task1.md` 是 Git 已追蹤檔。

```text
$ git log --diff-filter=A --format="%H %s" -- INVENTORY_task1.md
4572a4917e665085bce59f873984a4f1e1ce16f0 完成：交付成果與檢討
```

真正引入 commit 為 `4572a4917e665085bce59f873984a4f1e1ce16f0`。這裡刻意使用 `--diff-filter=A`，避免把最後修改 commit 誤當引入 commit。

```text
$ git show --stat --oneline --no-renames 4572a4917e665085bce59f873984a4f1e1ce16f0 -- INVENTORY_task1.md
4572a49 完成：交付成果與檢討
 INVENTORY_task1.md | 64 ++++++++++++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 64 insertions(+)
```

## 軟證據風險

`origin/main` 對齊狀態：成功取得，但不作為 #1 硬門檻。

```text
$ git rev-list --left-right --count origin/main...HEAD
0	3
```

執行時本地 `HEAD` 比 `origin/main` ahead 3。這是分支狀態風險，不否定硬門檻。

```text
$ git log --oneline origin/main..HEAD
86dca9a 任務#1 第2輪：驗證 `INVENTORY_task1.md` 的追蹤狀態、引入 commit、origin/main 對齊與目前引用
75f968b 任務#1 第1輪：驗證 `INVENTORY_task1.md` 的追蹤狀態、引入 commit、origin/main 對齊與目前引用
72058ef 架構決策：記錄 ADR
```

```text
$ git diff --stat origin/main..HEAD -- INVENTORY_task1.md

```

ahead commits 沒有修改 `INVENTORY_task1.md`。

```text
$ git branch -r --contains 4572a4917e665085bce59f873984a4f1e1ce16f0
  origin/HEAD -> origin/main
  origin/main
  ti_publish/ti-studio/ap9b8bc08e04
```

`origin/main` 包含引入 commit，代表此檔不是本機未提交暫存檔。

目前引用只列現況，不要求清零：

```text
$ rg -n "INVENTORY_task1(\\.md)?" . --glob "!.git/**" --glob "!node_modules/**" --glob "!dist/**" --glob "!build/**"
./DECISIONS.md:1419:## 沿用：範圍邊界 = 只處理 staged/working tree 層級；已 commit 進 origin/main 的 INVENTORY_task1.md 等明確排除。
./DECISIONS.md:1544:## Issue 前置證據必用只讀指令核對：`git show --stat 4572a49`、`git log -1 --oneline -- INVENTORY_task1.md`、`git ls-files --stage INVENTORY_task1.md`。
./DECISIONS.md:1552:## 引用搜尋固定用 `rg -n "INVENTORY_task1\.md|INVENTORY_task1" .`，並排除 `.git` 與建置輸出目錄。
./DECISIONS.md:1557:## `INVENTORY_task1.md` 判定為一次性盤點/驗收基準文件，非產品文件。
./DECISIONS.md:1561:## 移除 PR 預設只做 `git rm INVENTORY_task1.md`，不得順手修改非必要檔案。
./DECISIONS.md:1569:## Issue 必附可複製指令：`git rm INVENTORY_task1.md`、`git status --short`、`git ls-files --error-unmatch INVENTORY_task1.md`、引用搜尋指令。
./KNOWN_LIMITATIONS.md:5:- [ ] 盤點 INVENTORY_task1.md／README.md／ci.yml 等的追蹤與提交狀態，將「已提交的不相關檔」明列為範圍外待辦交回 PM
./VERIFICATION_REPORT_inventory_task1.md:1:# INVENTORY_task1.md 最小可解鎖證據包
./VERIFICATION_REPORT_task4.md:108:**目的**：盤點 INVENTORY_task1.md / README.md / ci.yml 等的「追蹤狀態 + 引入 commit + 與 origin/main 的對齊關係」。
./VERIFICATION_REPORT_task4.md:114:| `INVENTORY_task1.md` | Y | `4572a49` 完成：交付成果與檢討 | Y（4572a49 → a5dc6b3 merge） | 64 行；本任務範圍外 |
./VERIFICATION_REPORT_task4.md:138:| INVENTORY_task1.md 追蹤狀態 | 已 commit 於「完成：交付成果與檢討」 | 一致 | 沿用 |
./VERIFICATION_REPORT_task4.md:156:| `INVENTORY_task1.md` | `4572a49` 完成：交付成果與檢討 | repo root | 64 行 |
./VERIFICATION_REPORT_task4.md:236:for f in INVENTORY_task1.md README.md .github/workflows/ci.yml scripts/redeploy.sh studio/autopilot.py DECISIONS.md adr.json; do
./adr.json:2377:      "decision": "沿用：範圍邊界 = 只處理 staged/working tree 層級；已 commit 進 origin/main 的 INVENTORY_task1.md 等明確排除。",
./adr.json:2580:      "decision": "Issue 前置證據必用只讀指令核對：`git show --stat 4572a49`、`git log -1 --oneline -- INVENTORY_task1.md`、`git ls-files --stage INVENTORY_task1.md`。",
./adr.json:2594:      "decision": "引用搜尋固定用 `rg -n \"INVENTORY_task1\\.md|INVENTORY_task1\" .`，並排除 `.git` 與建置輸出目錄。",
./adr.json:2601:      "decision": "`INVENTORY_task1.md` 判定為一次性盤點/驗收基準文件，非產品文件。",
./adr.json:2608:      "decision": "移除 PR 預設只做 `git rm INVENTORY_task1.md`，不得順手修改非必要檔案。",
./adr.json:2622:      "decision": "Issue 必附可複製指令：`git rm INVENTORY_task1.md`、`git status --short`、`git ls-files --error-unmatch INVENTORY_task1.md`、引用搜尋指令。",
```

## 是否解鎖 #2

解鎖。`INVENTORY_task1.md` 已被 Git 追蹤、對 `HEAD` 無目標檔差異，且引入 commit 明確為 `4572a4917e665085bce59f873984a4f1e1ce16f0`。`origin/main...HEAD = 0 3` 與現有引用清單只作為後續 issue/PR 風險，不阻塞文件屬性判定。

## 可重跑自測

```sh
timeout 60 bash -lc 'set -euo pipefail
test -s VERIFICATION_REPORT_inventory_task1.md
git diff --check -- VERIFICATION_REPORT_inventory_task1.md
git diff --quiet HEAD -- INVENTORY_task1.md
git ls-files --error-unmatch INVENTORY_task1.md >/dev/null
test "$(git log --diff-filter=A --format=%H -- INVENTORY_task1.md)" = "4572a4917e665085bce59f873984a4f1e1ce16f0"
git rev-list --left-right --count origin/main...HEAD >/tmp/inventory_task1_origin_alignment.out || echo "origin/main alignment: undetermined" >/tmp/inventory_task1_origin_alignment.out
git diff --quiet origin/main..HEAD -- INVENTORY_task1.md
rg -n "INVENTORY_task1(\\.md)?" . --glob "!.git/**" --glob "!node_modules/**" --glob "!dist/**" --glob "!build/**" >/tmp/inventory_task1_refs.out'
```

此自測刻意不檢查 `test ! -e INVENTORY_task1.md` 或「不被追蹤」，那是 #5 移除後驗收，不屬於 #1。
