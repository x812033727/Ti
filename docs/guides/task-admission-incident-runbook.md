# Task-admission incident 處置手冊

本手冊只處理 task-admission mode control 與 incident ledger。它不提供一鍵修復，也不允許用
restart 掩蓋根因。Admission fault 時 worker 會 fail-closed 停止新任務 intake，但既有 Web
服務仍健康，因此公開 `/api/health` 維持 HTTP 200／`ok: true`，另回
`status: "degraded"`、`intake_available: false` 與穩定 `error_code`。

## 先確認事故範圍

1. 公開 health 只確認 `status`、`intake_available`、`error_code`，不含路徑或 control 內容。
2. 以已登入的 `/api/autopilot` 查看 `task_admission_incident`：incident ID、first/last seen、
   duration、最後已知 effective mode/generation 與 durability。
3. `mode_switch_wait` 是正常安全邊界等待，不是 incident；不要 page、quarantine 或 restart。
4. Layer 3 對 `admission_mode_fault` 只顯示 degraded。外部通知由 Python worker 單獨負責，
   Layer 3 不再送 webhook、不喚起 Claude，也不 restart。

## 哪些錯誤可 quarantine

只有已確認為「內容損壞」的 control state 才可 quarantine，例如：

- `invalid_json`、`invalid_schema`、`invalid_mode`
- `invalid_generation`、`invalid_effective_generation`、`generation_regressed`
- `inconsistent_ack`
- `invalid_requested_at`、`invalid_applied_at`、`invalid_released_holds`
- `state_too_large`

下列類型不可用 quarantine 規避，必須修正底層問題並保留原檔：

- owner／hardlink／symlink 或 lock 安全錯誤
- state directory、lock、檔案不可讀寫
- disk full、read-only filesystem、inode 耗盡
- backlog commit barrier 或 hold release 失敗

若錯誤碼不足以區分內容損壞與權限／儲存問題，先以 `stat`、filesystem 容量及 service journal
確認；不要猜測後直接移檔。

## 原子 quarantine control state

前提：`admission_mode.lock` 本身是可信的一般檔案，且已排除 owner、symlink、hardlink 與儲存
故障。以下操作會取得與 worker 相同的 lock，並在同一目錄原子 rename；**不得用 `rm`**。

```sh
ADMISSION_STATE_DIR=/opt/ti/autopilot
sudo stat -c '%F owner=%U mode=%a links=%h %n' \
  "$ADMISSION_STATE_DIR/admission_mode.lock" \
  "$ADMISSION_STATE_DIR/admission_mode.json"
sudo flock "$ADMISSION_STATE_DIR/admission_mode.lock" sh -c '
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  mv -- "$1/admission_mode.json" \
    "$1/admission_mode.json.quarantine.$stamp"
' sh "$ADMISSION_STATE_DIR"
```

不要重啟 worker。下一個安全任務邊界會看到 control state 遺失，從 worker 的 runtime effective
pin 重建 desired/effective；只有 runtime pin 已更新、control state healthy、generation 不再
pending，且 intake guard 準備回傳成功時，才會關閉 incident 並送一次 recovery。

## Incident ledger 損壞

`admission_incident.json` 與 control state 完全分離。它寫入失敗或損壞時，worker 仍會 page，
並在同一行程用記憶體去重；跨行程重啟可能再通知一次，這是刻意的 fail-loud 取捨。

確認是 JSON/schema 內容損壞時，先檢查 ledger 與兩把 lock 都是可信的一般檔案且 link count
為 1，再依 delivery → ledger 的固定 lock order 取得鎖並原子 quarantine：

```sh
ADMISSION_STATE_DIR=/opt/ti/autopilot
sudo stat -c '%F owner=%U mode=%a links=%h %n' \
  "$ADMISSION_STATE_DIR/admission_incident_delivery.lock" \
  "$ADMISSION_STATE_DIR/admission_incident.lock" \
  "$ADMISSION_STATE_DIR/admission_incident.json"
sudo flock "$ADMISSION_STATE_DIR/admission_incident_delivery.lock" \
  flock "$ADMISSION_STATE_DIR/admission_incident.lock" sh -c '
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  mv -- "$1/admission_incident.json" \
    "$1/admission_incident.json.quarantine.$stamp"
' sh "$ADMISSION_STATE_DIR"
```

若是 owner、symlink、lock 或 storage fault，仍須修正底層問題，不得以 rename 繞過。下一次
worker observation 會重試把 process-local lifecycle 原子寫回 ledger。

## 驗收

- `/api/health` 回 `status: "ok"`、`intake_available: true`、空 `error_code`。
- `/api/autopilot.task_admission_mode_state` 為 healthy、pending false，effective generation
  已追上 desired generation。
- `/api/autopilot.task_admission_incident` 為 active false，且 recovered timestamp 存在。
- 通知記錄只有首次 fault、每次 fault code 改變，以及一次 recovery；相同 error code 重試不洗版。
- Quarantine 檔仍留在原目錄供鑑識，沒有任何原檔被刪除。

通知採 durable outbox 的 at-least-once 語意。若行程恰好在外部 adapter 接受事件後、local ack
前崩潰，重啟後可能以同一 `event_id` 重送；這個極短重複窗口優先於靜默漏報。
