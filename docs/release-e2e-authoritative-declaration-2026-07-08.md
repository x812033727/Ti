# Release E2E 最終權威補充宣告（2026-07-08）

本檔是 additive 補充宣告；不覆寫既有 `docs/release-e2e-closure-report.md`、
`docs/release-e2e-handoff.md` 或 `docs/evidence/` 內任一憑證檔。

## 權威結論

- 最終權威宣告檔：`725cf1`
- 最終權威宣告檔整檔 sha256：`99f330…9d3b`
- 被重跑取代的前一輪識別：`c2f4bb`
- 重跑鏈：`c2f4bb→725cf1`

以上三個值 `c2f4bb`、`725cf1`、`99f330…9d3b` 逐字採用 QA 訊息流明列值；
其中 `99f330…9d3b` 的省略號保留原樣，不在本檔自行展開或推算。

## 重跑事實與原因

`c2f4bb→725cf1` 不是單純標成「已修正」的例行重產，而是因前一輪宣告未把
整檔 sha256、檔內嵌 hash 與防覆寫第二信源的權威層級說清楚，容易讓讀者誤以為
檔內自填 hash 可以替代外部整檔校驗。為避免後續驗收把「檔內自證」誤讀成
「權威身分」，本輪改以 `725cf1` 作為最終權威宣告檔，並明列其整檔 sha256
為 `99f330…9d3b`。

時序四要素如下：

1. 起點：前一輪產物識別為 `c2f4bb`。
2. 觸發原因：語義未清楚區分「整檔 sha256（權威）」與「檔內嵌 hash（僅自證）」，
   且防覆寫第二信源未明確指定以 QA 訊息流明列值為準。
3. 重跑行動：依 QA 指示重跑並補足權威語義，不改動 closure、handoff 與 evidence 檔。
4. 最終落點：`725cf1` 被宣告為最終權威宣告檔，整檔 sha256 為 `99f330…9d3b`。

## Hash 語義

- 整檔 sha256（權威）：針對宣告檔完整位元組計算，是判定最終權威檔身分的外部校驗值；
  本輪權威值為 `725cf1` 對應的 `99f330…9d3b`。
- 檔內嵌 hash（僅自證）：只代表檔案內容中寫入的自我描述值；若檔案被覆寫後仍保留舊字串，
  它不能單獨證明整檔未被替換，因此不得取代整檔 sha256。
- 防覆寫第二信源：以 QA 訊息流明列值為準；當檔內敘述、衍生輸出或人工摘要互相衝突時，
  以 QA 訊息流明列的 `c2f4bb`、`725cf1`、`99f330…9d3b` 為優先對照來源。

## 自驗指令

```bash
.venv/bin/python -m pytest tests/docs -q
python3 - <<'PY'
from hashlib import sha256
from pathlib import Path

path = Path("docs/release-e2e-authoritative-declaration-2026-07-08.md")
print(sha256(path.read_bytes()).hexdigest())
PY
```
