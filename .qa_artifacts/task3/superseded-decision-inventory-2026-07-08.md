# Task3 被作廢決議檔識別碼與 hash 清單（2026-07-08）

本清單供 `docs/task3-authoritative-decision-2026-07-08.md` 落盤時逐字引用。
來源限定為本輪 QA 訊息流與 repo 內已存在的宣告檔；含省略號的值原樣保留，不展開、不推算。

## 蒐集結果

目前可確認的 task3 被作廢決議檔為 2 份。可見 QA 訊息流只明列 `778ced` 與 `rerun-765f1b` 兩個 task3 短碼；未明列另外 3 份獨立 task3 決議檔識別碼，故本清單不得以其他任務線的值湊滿五份。

| # | QA 訊息流逐字值 | 類型 | hash 關聯 | repo 實際路徑或落點判定 |
|---|---|---|---|---|
| 1 | `778ced` | 決議檔識別碼 | 訊息流未明列；不得推算 | 訊息流明列值；repo 內未找到對應實體決議檔 |
| 2 | `rerun-765f1b` | 重跑決議檔識別碼 | 訊息流未明列；不得推算 | 訊息流明列值；repo 內未找到對應實體決議檔 |

## 實體路徑判定

- `778ced`、`rerun-765f1b`：本輪 repo 搜尋不到同名實體決議檔，依架構決策宣告為「QA 訊息流明列值、非 repo 實體檔」。

## 排除為 task3 決議檔的值

- `c2f4bb`：出現於 `docs/release-e2e-authoritative-declaration-2026-07-08.md`，語境是 release-e2e 前一輪識別，不是 task3 決議檔。
- `725cf1`：出現於 `docs/release-e2e-authoritative-declaration-2026-07-08.md`，語境是 release-e2e 最終權威宣告檔識別，不是 task3 決議檔。
- `99f330…9d3b`：出現於 `docs/release-e2e-authoritative-declaration-2026-07-08.md`，語境是 `725cf1` 的整檔 sha256；它是 hash 屬性，不是另一份獨立決議檔。

## 引用約束

- 下游權威檔作廢清單只能把 `778ced`、`rerun-765f1b` 當作已確認的 task3 被作廢決議檔。
- 若下游仍需五份 task3 決議檔，須由 QA 訊息流補齊另外 3 份獨立決議檔識別碼與 hash 關聯；本清單不得推算或借用 release-e2e 值。
- `99f330…9d3b` 的省略號是 U+2026，禁止改成三個句點 `...`。
- 未明列的 hash 不補值、不算值、不用短碼推導。
