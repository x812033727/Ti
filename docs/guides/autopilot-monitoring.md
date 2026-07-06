# Autopilot 心跳監控判定規則

本指南給**外部監控腳本**（如「層3監控」）作者：如何正確讀 autopilot 的 `status.json`
心跳，避免把長輪多專家討論誤判成死鎖而 restart（見 issue #285——同一根因曾在一天內兩次
誤報並 restart，丟失數小時任務進度）。

心跳由 `studio.autopilot` 每輪主迴圈寫一次；任務執行中改為**事件驅動**刷新——專家發言完成
（`final` 的 `expert_message`）與關鍵工具事件（`tool_use`）會即時更新 `last_activity_at` 與
目前專家 turn，不必等背景 tick；另有背景任務每 ~60s 保底刷新（涵蓋長 thinking／單則超長串流
的無事件窗）。狀態落在 `<AUTOPILOT_STATE_DIR>/status.json`，並由 `GET /api/autopilot` 原樣
併入回應的 `heartbeat` 欄位（免直接讀檔）。

## `status.json` 欄位

| 欄位 | 型別 | 意義 |
|------|------|------|
| `state` | str | `idle` / `running` / `quota_sleep` / `budget_sleep` / `rotate_restart` / `stopped` |
| `task_id` | int/str/null | 當前任務 id（`running` 時） |
| `sleep_until` | float/null | 睡到何時（quota/budget/rotate sleep） |
| `updated_at` | float | 每次寫入的 epoch 秒——**主迴圈存活訊號**，任務中每 ~60s 前進一次 |
| `quota` | dict | 各 provider 用量快照 |
| `last_activity_at` | float/null | 最近一次「有進展」的 epoch 秒——**事件驅動**取工具/發言事件時間，60s 保底 tick 另取當前 session events jsonl 檔 mtime 的較新者。判新鮮度**以此欄為準**，取代早期掃 journal 間接推斷 |
| `current_expert` | str/null | 目前輪到的專家 key（turn 進行中）；任務收尾或無 turn 時為 `null` |
| `turn_started_at` | float/null | 目前專家 turn 的起始 epoch 秒；`now - turn_started_at` 即該專家已跑時長。無 turn 時為 `null` |
| `workers` | dict/null | 子行程活性；`{"count": int\|null, "cpu_active": bool\|null}` |

### `current_expert` / `turn_started_at`（專家 turn 粒度）

- turn 起點由事件推斷：取 `tool_use` 或 `final` `expert_message` 中**先出現的新 speaker** 為
  `turn_started_at`（抵消「先靜默跑工具、稍後才發言」的延遲），`current_expert` 記該 speaker key。
- 同一專家連續事件不重開 turn；換到新 speaker 才前進 `current_expert` 並重置 `turn_started_at`。
- 任務收尾（含 `_select_workflow`/clone 失敗提早 return 的路徑）清為 `null`，避免上一任務殘留。
- 舊 `status.json` / 舊 JSONL 無此兩欄一律視為 `null`，監控端須 null-safe 讀取，不得因缺欄報錯。
- 這兩欄是**觀測輔助**（timeline 顯示「目前輪到誰＋已跑多久」），**不參與判死**——判死只看
  `updated_at` / `last_activity_at` / `workers.cpu_active`（見下方規則）。

### `workers`（子行程活性）

- `count`：autopilot 主行程的存活後裔子行程數（LLM 專家子行程等）。
- `cpu_active`：跨兩次 ~60s 心跳 tick，任一子行程的 CPU tick（utime+stime）是否前進。
  - `true`＝**有 worker 正在燒 CPU＝非死鎖**（即使 `last_activity_at` 凍結）。
  - `false`＝子行程存活但該窗未見 CPU 前進。
  - `null`＝**無法判定**：`/proc` 不可用（非 Linux）、取樣失敗，或任務剛起的第一個 tick
    （尚無前次快照可比）。
- 非任務狀態（`idle`/`quota_sleep`…）`workers` 為 `null`。

`workers.cpu_active` 存在的理由：專家只在每則 SDK 訊息 / 工具呼叫時才產出事件；單一長工具
呼叫、長 thinking、或單則超長串流期間**完全無事件**，`last_activity_at`（事件驅動值與 events
mtime 皆凍結）會停滯 30–90 分鐘，但子行程其實持續在算。`cpu_active` 以子行程 CPU 取樣補足這個
盲區，與事件粒度解耦。**故 `last_activity_at` 長不動不能單獨判死，必須與 `cpu_active == false`
同時成立。**

## 判定規則（監控腳本請照此實作）

> **新鮮度來源**：一律讀 `status.json` 的 `last_activity_at`（事件驅動＋60s 保底），**不要**
> 自行掃 `history/<id>.jsonl` journal 的 mtime 或行數去間接推斷活性——那是早期做法，已被
> `last_activity_at` 取代，兩者並用只會製造分歧。判活性只看下列欄位。

1. **主迴圈存活**：`updated_at` 應每 ≤60s 前進。若停滯超過門檻（建議數分鐘含裕度）⇒
   主迴圈可能真的死了，可 restart。

2. **任務存活（避免長 inter-message 誤報）**：`state == "running"` 時，只要
   **`workers.cpu_active == true`** 或 **`last_activity_at` 仍在前進**，即代表仍在工作，
   **不得 restart**。

3. **僅在以下情況才判死鎖 restart**（AND 邏輯，不放寬）：
   - `updated_at` 停滯（主迴圈死）；**或**
   - `state == "running"` 且 `workers.cpu_active == false` **且** `last_activity_at` 長時間不動。
   - 第二條的兩個子句必須**同時**成立才殺——`cpu_active == true` 或 `last_activity_at` 仍前進
     任一為真即不得 restart（對應 issue #285 誤殺教訓）。門檻建議 ≥3× 刷新間隔（≥3–5 分含裕度）。

4. **`workers.cpu_active == null` 不可單獨作為 restart 依據**（`/proc` 不可用或首 tick 屬正常）——
   此時退回規則 2/3 用 `updated_at` 與 `last_activity_at` 判斷。

5. **`current_expert` / `turn_started_at` 僅供觀測**（顯示「目前輪到誰＋已跑多久」），
   **不得**作為判死依據——長 turn 本就可能長時間停在同一專家，據此 restart 會重演誤殺。

## 讀取範例

```sh
# 經 API（建議）
curl -s http://127.0.0.1:8021/api/autopilot | jq '.heartbeat | {state, updated_at, last_activity_at, current_expert, turn_started_at, workers}'
# → {"state":"running","updated_at":1783140425.1,"last_activity_at":1783137605.0,"current_expert":"senior","turn_started_at":1783137600.0,"workers":{"count":5,"cpu_active":true}}
```

上例即本次誤報情境：`last_activity_at` 已凍結約 47 分鐘（長 inter-message 間隔），但
`workers.cpu_active == true` 明確證明「有 5 個 worker 在燒 CPU、非死鎖」——依規則 2 **不得 restart**。
`current_expert` / `turn_started_at` 顯示目前輪到 `senior`、已跑 `now - turn_started_at`（供
timeline 展示），但如規則 5 所述**不參與判死**。

相關程式碼：`studio/autopilot.py` 的 `_proc_descendant_cpu` / `_workers_field` /
`_task_heartbeat` / `_write_status`；API 於 `studio/routes.py::autopilot_status`。
