# Task3 唯一權威決議（2026-07-08）

本檔是 additive 權威決議；不覆寫既有 `docs/release-e2e-closure-report.md`、
`docs/release-e2e-handoff.md`、`docs/evidence/`，也不改寫任何既有決議檔歷史內容。

## 權威結論

- 唯一權威決議檔：`docs/task3-authoritative-decision-2026-07-08.md`
- QA 回報權威檔相對路徑：`docs/task3-authoritative-decision-2026-07-08.md`
- 來源：`.qa_artifacts/task3/superseded-decision-inventory-2026-07-08.md`
- 來源型態：QA 訊息流明列值；五列採固定占位契約，不查找、不展開、不推算。
- repo 實體檔狀態：五列皆為 `訊息流明列值／查無 repo 實體檔`。

## 被作廢決議清單

每列表格的作廢標記欄皆填同一個 ADR Superseded-by 值。
`<訊息流未明列>` 與 `訊息流明列值／查無 repo 實體檔` 為固定 token，逐字保留。

| 序號 | 識別碼 | hash | 路徑欄 | 作廢標記 |
|---|---|---|---|---|
| 1 | 778ced | <訊息流未明列> | 訊息流明列值／查無 repo 實體檔 | Superseded by docs/task3-authoritative-decision-2026-07-08.md |
| 2 | rerun-765f1b | <訊息流未明列> | 訊息流明列值／查無 repo 實體檔 | Superseded by docs/task3-authoritative-decision-2026-07-08.md |
| 3 | <訊息流未明列> | <訊息流未明列> | 訊息流明列值／查無 repo 實體檔 | Superseded by docs/task3-authoritative-decision-2026-07-08.md |
| 4 | <訊息流未明列> | <訊息流未明列> | 訊息流明列值／查無 repo 實體檔 | Superseded by docs/task3-authoritative-decision-2026-07-08.md |
| 5 | <訊息流未明列> | <訊息流未明列> | 訊息流明列值／查無 repo 實體檔 | Superseded by docs/task3-authoritative-decision-2026-07-08.md |

## 雙向連結與 immutability

- 權威檔到被作廢端：以上五列逐字列出被作廢識別、hash 欄與路徑欄。
- 被作廢端到權威檔：若 repo 內存在原決議檔，該端應回指本權威檔；本輪五列皆查無 repo 實體檔，因此不新增、不改寫、不偽造原檔。
- 後續待辦：若日後補出任一原決議實體檔，須在該檔回填反向回指本權威檔；未補出前不建立替身檔。
- release-e2e 的 `c2f4bb`、`725cf1`、`99f330…9d3b` 僅作排除說明；`99f330…9d3b` 的省略號為 U+2026，禁止改成三個連續半形句點，且不得當作第五份 task3 決議檔。

## Hash 語義

- 整檔 sha256（權威）：針對本檔完整 bytes 由檔外重算與回報；此值不可固定嵌回本檔，避免自我指涉導致雜湊不穩定。
- 檔內嵌 hash（僅自證）：檔內出現的 hash 或省略 hash 僅是文字證據，不能取代整檔 sha256，也不能被展開或推算成 64-hex。
- 本檔不把 `99f330…9d3b` 視為 task3 權威整檔 sha256；它是 release-e2e 宣告鏈的逐字值。

## 原子落盤紀錄

本檔以標準庫流程落盤：在目標目錄建立暫存檔，寫入後 `flush`、`os.fsync`，再以 `os.replace` 取代目標檔，最後 `fsync` 父目錄；未新增第三方依賴。

## 自驗指令

```bash
.venv/bin/python -m pytest tests/docs -q
python3 - <<'PY'
from hashlib import sha256
from pathlib import Path

path = Path("docs/task3-authoritative-decision-2026-07-08.md")
print(sha256(path.read_bytes()).hexdigest())
PY
```
