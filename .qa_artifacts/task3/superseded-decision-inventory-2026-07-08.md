# Task3 被作廢決議檔識別碼與 hash 清單（2026-07-08）

本清單供 `docs/task3-authoritative-decision-2026-07-08.md` 落盤時逐字引用。
來源限定為本輪 QA 訊息流與 repo 內已存在的宣告檔；含省略號的值原樣保留，不展開、不推算。

## 蒐集結果

| # | QA 訊息流逐字值 | 類型 | hash 關聯 | repo 實際路徑或落點判定 |
|---|---|---|---|---|
| 1 | `778ced` | 決議檔識別碼 | 訊息流未明列；不得推算 | 訊息流明列值；repo 內未找到對應實體決議檔 |
| 2 | `rerun-765f1b` | 重跑決議檔識別碼 | 訊息流未明列；不得推算 | 訊息流明列值；repo 內未找到對應實體決議檔 |
| 3 | `c2f4bb` | 被重跑取代的前一輪識別碼 | 訊息流未明列；不得推算 | `docs/release-e2e-authoritative-declaration-2026-07-08.md` |
| 4 | `725cf1` | 最終權威宣告檔識別碼 | `99f330…9d3b` | `docs/release-e2e-authoritative-declaration-2026-07-08.md` |
| 5 | `99f330…9d3b` | 省略 sha/hash 逐字值 | 僅作訊息流明列 hash；不得展開成 64 碼 | `docs/release-e2e-authoritative-declaration-2026-07-08.md` |

## 實體路徑判定

- `778ced`、`rerun-765f1b`：本輪 repo 搜尋不到同名實體決議檔，依架構決策宣告為「QA 訊息流明列值、非 repo 實體檔」。
- `c2f4bb`、`725cf1`、`99f330…9d3b`：repo 內出現於 `docs/release-e2e-authoritative-declaration-2026-07-08.md`；該檔是既有 additive 宣告範式來源，不代表本輪要改寫原內容。

## 引用約束

- 下游權威檔作廢清單須逐字包含以上五個值。
- `99f330…9d3b` 的省略號是 U+2026，禁止改成三個句點 `...`。
- 未明列的 hash 不補值、不算值、不用短碼推導。
