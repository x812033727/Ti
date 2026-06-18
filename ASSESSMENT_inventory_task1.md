# INVENTORY_task1.md 文件屬性判定

日期：2026-06-18

執行時 HEAD：`28c46d421c4d3f92c6e8d6b1582fbeae739aae18`

## 結論

`INVENTORY_task1.md` 不應長期留在 repo root。

它是任務 #1 的一次性盤點與驗收基準，內容用來支撐當時「裸 `python` 命中分類」的修正範圍，不是產品說明、開發者入口、架構文件、操作手冊或持續維護的專案文件。

本輪只完成文件屬性判定與保留/移除理由，不執行 `git rm INVENTORY_task1.md`。移除應交給後續獨立 issue/commit/PR。

## 證據

- `INVENTORY_task1.md` 目前是 Git 已追蹤檔，不是未提交暫存檔。
- 引入 commit：`4572a4917e665085bce59f873984a4f1e1ce16f0 完成：交付成果與檢討`。
- 引入內容只有 `INVENTORY_task1.md`，共 64 行新增：

```text
4572a49 完成：交付成果與檢討
 INVENTORY_task1.md | 64 ++++++++++++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 64 insertions(+)
```

- 既有決策已將它標為「一次性盤點/驗收基準文件，非產品文件」，並要求後續用 PR 化移除。
- 既有引用集中在決策、ADR、驗證報告與限制清單，屬治理/追溯用途；引用存在不代表此檔應保留在 repo root。

## 保留理由

- 短期保留可讓後續 issue/PR 直接引用完整證據，避免把已追蹤檔誤判成暫存垃圾。
- 它仍可作為移除前的審核上下文，說明當初 task #1 的盤點範圍與驗收 oracle。
- 目前有治理文件引用它；移除前需要在 issue 內列出哪些引用保留為歷史紀錄、哪些引用需要同步調整。

## 移除理由

- repo root 應保留長期入口文件；此檔只服務單一任務的過程驗收，任務結束後價值遞減。
- 檔名與內容都綁定 `task1`，後續讀者容易誤以為它是仍需維護的全 repo inventory。
- 它描述的是過去某輪裸 `python` 修正基準，不是當前產品行為；長期保留會增加 root 雜訊與錯誤引用風險。
- 已有 `DECISIONS.md`、`adr.json`、`VERIFICATION_REPORT_task4.md`、`VERIFICATION_REPORT_inventory_task1.md` 承接必要追溯資訊，不需要把一次性盤點檔長期放在 root。

## 後續建議

- #3 建立移除 issue，標題建議：`Remove obsolete task inventory INVENTORY_task1.md`。
- issue 內容需列完整引入 commit、此判定文件、移除方式 `git rm INVENTORY_task1.md`、驗收指令與引用清單處理策略。
- #4 才執行獨立 commit 移除；預設只刪 `INVENTORY_task1.md`，除非 issue 明確要求同步修改引用。
- #5 驗收再檢查檔案不存在、Git 不再追蹤，以及引用只剩歷史或同步說明。

## 可重跑自測

```sh
timeout 60 bash -lc 'set -euo pipefail
test -s ASSESSMENT_inventory_task1.md
git diff --check -- ASSESSMENT_inventory_task1.md
git ls-files --error-unmatch INVENTORY_task1.md >/dev/null
test "$(git log --diff-filter=A --format=%H -- INVENTORY_task1.md)" = "4572a4917e665085bce59f873984a4f1e1ce16f0"
rg -n "一次性盤點|不應長期留在 repo root|git rm INVENTORY_task1.md" ASSESSMENT_inventory_task1.md >/dev/null'
```
