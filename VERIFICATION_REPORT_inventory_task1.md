# INVENTORY_task1.md 追蹤狀態驗證

日期：2026-06-18

## 結論

- `INVENTORY_task1.md` 存在於 repo root，64 行，已由 Git 追蹤。
- 該檔對 `HEAD` 無修改：`git diff HEAD -- INVENTORY_task1.md` 無輸出。
- 引入 commit 為 `4572a4917e665085bce59f873984a4f1e1ce16f0`（短 SHA：`4572a49`，訊息：`完成：交付成果與檢討`）。
- `origin/main` 包含 `4572a49`，所以此檔不是未提交暫存檔。
- 目前本地 `main` 與 `origin/main` 不完全對齊：`origin/main...HEAD` 為 `0 1`，本地多出 `72058ef 架構決策：記錄 ADR`；但該 ahead commit 未修改 `INVENTORY_task1.md`。

## 必要指令證據

```text
$ git status --short --branch
## main...origin/main [ahead 1]
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
0	1
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
- `VERIFICATION_REPORT_task4.md`：既有驗證表列此檔為已 commit 進 `origin/main`。

本報告建立後，上述搜尋也會命中 `VERIFICATION_REPORT_inventory_task1.md`，屬本輪新增證據引用。
