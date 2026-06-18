# INVENTORY_task1.md 追蹤狀態驗證

日期：2026-06-18

## 結論

- `INVENTORY_task1.md` 存在於 repo root，64 行，已由 Git 追蹤。
- 該檔對 `HEAD` 無修改：`git diff HEAD -- INVENTORY_task1.md` 無輸出。
- 引入 commit 為 `4572a4917e665085bce59f873984a4f1e1ce16f0`（短 SHA：`4572a49`，訊息：`完成：交付成果與檢討`）。
- `origin/main` 包含 `4572a49`，所以此檔不是未提交暫存檔。
- 第 2 輪重跑時，本地 `main` 與 `origin/main` 不完全對齊：`origin/main...HEAD` 為 `0 2`。
- ahead commits 為 `75f968b` 與 `72058ef`，兩者都未修改 `INVENTORY_task1.md`；判斷重點是 `origin/main` 已包含引入 commit，且 `origin/main..HEAD -- INVENTORY_task1.md` 無差異。
- `origin/main...HEAD` 是會隨本報告後續提交改變的分支快照；驗收時應重跑本節指令，不把舊快照視為固定常數。

## 必要指令證據

以下輸出是第 2 輪修正本報告前的只讀重跑快照。套用或提交本報告修正後，`git status` 與 `origin/main...HEAD` 可能因報告本身變更而不同；目標檔判斷仍以 `git diff HEAD -- INVENTORY_task1.md` 與 `git diff --stat origin/main..HEAD -- INVENTORY_task1.md` 無輸出為準。

```text
$ git status --short --branch
## main...origin/main [ahead 2]
```

```text
$ git status --porcelain

```

```text
$ git diff HEAD -- INVENTORY_task1.md

```

```text
$ git ls-files --stage INVENTORY_task1.md
100644 b0d7082eaea5885e35b7e23b8ce80fd14c016a77 0	INVENTORY_task1.md
```

```text
$ git log -1 --oneline -- INVENTORY_task1.md
4572a49 完成：交付成果與檢討
```

```text
$ git show --stat --oneline --no-renames 4572a49 -- INVENTORY_task1.md
4572a49 完成：交付成果與檢討
 INVENTORY_task1.md | 64 ++++++++++++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 64 insertions(+)
```

## origin/main 對齊證據

```text
$ git rev-list --left-right --count origin/main...HEAD
0	2
```

```text
$ git log --oneline origin/main..HEAD
75f968b 任務#1 第1輪：驗證 `INVENTORY_task1.md` 的追蹤狀態、引入 commit、origin/main 對齊與目前引用
72058ef 架構決策：記錄 ADR
```

```text
$ git branch -r --contains 4572a49
  origin/HEAD -> origin/main
  origin/main
  ti_publish/ti-studio/ap9b8bc08e04
```

```text
$ git log --oneline origin/main -- INVENTORY_task1.md | head -5
4572a49 完成：交付成果與檢討
```

```text
$ git diff --stat origin/main..HEAD -- INVENTORY_task1.md

```

## 既有引用

交付前搜尋指令：

```text
rg -n "INVENTORY_task1\.md|INVENTORY_task1" . --glob '!.git/**' --glob '!node_modules/**' --glob '!dist/**' --glob '!build/**'
```

命中摘要：

- `DECISIONS.md`：決策與 issue 前置證據要求。
- `KNOWN_LIMITATIONS.md`：待辦清單提到盤點此檔。
- `adr.json`：ADR 鏡像記錄。
- `VERIFICATION_REPORT_inventory_task1.md`：本輪追蹤狀態驗證報告。
- `VERIFICATION_REPORT_task4.md`：既有驗證表列此檔為已 commit 進 `origin/main`。

## 可重跑自測

交付前自測指令：

```sh
timeout 60 bash -lc 'set -euo pipefail
test -s VERIFICATION_REPORT_inventory_task1.md
git diff --check -- VERIFICATION_REPORT_inventory_task1.md
git diff --quiet HEAD -- INVENTORY_task1.md
git ls-files --error-unmatch INVENTORY_task1.md >/dev/null
test "$(git log -1 --format=%h -- INVENTORY_task1.md)" = "4572a49"
git branch -r --contains 4572a49 | grep -q "origin/main"
git diff --quiet origin/main..HEAD -- INVENTORY_task1.md
rg -n "INVENTORY_task1\\.md|INVENTORY_task1" . --glob "!.git/**" --glob "!node_modules/**" --glob "!dist/**" --glob "!build/**" >/tmp/inventory_task1_refs.out'
```

此指令刻意不要求整個工作目錄乾淨；本輪交付前，`git status --short` 會顯示本報告正在修改中。它只檢查 `INVENTORY_task1.md` 的驗證證據是否仍成立。
