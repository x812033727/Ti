# 端點盤點：本機限定（loopback）納管清單

> 任務 #5 產出。盤點 `studio/routes.py`（HTTP）與 `studio/ws.py`（WebSocket）所有入口，
> 標記「該限定本機」的端點與理由。事實依據以 `app.routes` 反查（含實際生效的 dependant），
> 非靠人工讀碼，避免漏列。

## 判定原則

- **納管（掛 `require_loopback`）**：會改機器狀態／控制面／可外洩秘密的**寫入**端點。
- **不納管**：讀取查詢、認證握手、靜態與框架端點。
- 信任判定統一走 `netutil.is_loopback`（spoof-safe、fail-closed），HTTP 用 `WRITE_DEPS` 依賴、
  WS 在 handler 內檢查（依賴注入對 WebSocket 不生效）。
- 回應碼：HTTP 403「僅限本機存取」；WS error payload 同字串後 `close(1008)`。

## HTTP 端點

| 方法 | 路徑 | 現況 deps | 納管 | 理由 |
|------|------|-----------|:----:|------|
| POST | `/api/redeploy` | loopback+auth | ✅ | 拉 main 並自我重啟，最高危機器狀態變更 |
| POST | `/api/auth/password` | loopback+auth | ✅ | 寫 .env 改存取密碼，秘密寫入面 |
| POST | `/api/settings` | loopback+auth | ✅ | 改 .env 設定（含 `OPENAI_BASE_URL` 等），可致金鑰外洩/RCE 風險 |
| POST | `/api/autopilot/pause` | loopback+auth | ✅ | 控制自動迴圈，遠端可癱瘓（DoS） |
| POST | `/api/autopilot/resume` | loopback+auth | ✅ | 控制自動迴圈狀態 |
| POST | `/api/autopilot/task` | loopback+auth | ✅ | 向會自主執行 bash 的 autopilot 注入任務 |
| GET  | `/api/settings` | auth | ➖ | 讀取設定（秘密欄位不回明文），敏感度低 |
| GET  | `/api/autopilot` | auth | ➖ | 讀取狀態 |
| GET  | `/api/autopilot/backlog` | auth | ➖ | 讀取待辦清單 |
| GET  | `/api/history` | auth | ➖ | 讀取歷史列表 |
| GET  | `/api/history/{session_id}/events` | auth | ➖ | 讀取單場事件 |
| GET  | `/api/workspace/{session_id}/files` | auth | ➖ | 讀取工作區檔案清單 |
| GET  | `/api/workspace/{session_id}/file` | auth | ➖ | 讀取單一檔案內容 |
| GET  | `/api/workspace/{session_id}/download` | auth | ➖ | 下載工作區壓縮檔 |
| GET  | `/api/publish/config` | auth | ➖ | 讀取發佈設定 |
| GET  | `/api/projects` | auth | ➖ | 讀取專案列表與 backlog 統計 |
| GET  | `/api/projects/{project_id}` | auth | ➖ | 讀取單一專案 meta 與 backlog |
| GET  | `/api/metrics` | auth | ➖ | 讀取運維指標（活躍場次/並發上限/history 計數/保留策略/workspace 數），無秘密 |
| POST | `/api/login` | — | ➖ | 認證握手，必須對外可達才能登入 |
| POST | `/api/logout` | — | ➖ | 認證握手 |
| GET  | `/api/auth/status` | — | ➖ | 公開狀態查詢（前端判斷是否需登入） |
| GET  | `/api/health` | — | ➖ | 健康檢查，需對外可達 |
| GET  | `/`, `/login` | — | ➖ | 前端 HTML 入口 |

### 寫入但**刻意不納管**（須記錄理由，供追溯）

| 方法 | 路徑 | 現況 deps | 理由 |
|------|------|-----------|------|
| POST | `/api/publish/{session_id}` | auth | session 結束後一次性「等 CI→合併」，僅作用於該 session 工作區、不改機器設定面；不在架構決策納管清單 |
| DELETE | `/api/history/{session_id}` | auth | 刪除歷史紀錄，作用於資料而非機器控制面；門禁已足夠 |
| POST | `/api/history/cleanup/completed` | auth | 清理已完成歷史，同上 |
| POST | `/api/history/cleanup/retention` | auth | 依保留策略回收超量/過舊歷史，作用於資料面而非機器控制面；同上 |
| POST | `/api/projects` | auth | 建立專案（寫 meta 與空 workspace 目錄），純資料面；與 /ws 同屬核心產品操作，須對已登入外網使用者可用 |
| POST | `/api/projects/{project_id}/backlog` | auth | 往「專案」backlog 排改良任務。與 autopilot 的任務注入端點（納管）不同：專案任務僅在已登入使用者經 /ws 主動啟動持續改良時才執行，且專家 bash 走 bwrap 沙箱（與 /ws 同安全模型），非無人值守自動執行 |

> 註：上列皆屬「資料面寫入」，架構決策的納管邊界限定在「會改機器狀態的控制面/秘密寫入」。
> 若後續威脅模型升級，可比照 `WRITE_DEPS` 一行掛上，已有守門測試結構可直接擴充。

## WebSocket 端點

| 端點 | 檢查方式 | 納管 | 理由 |
|------|----------|:----:|------|
| `/ws` | handler 內 `auth.is_authed`（共用密碼門禁） | ❌ | 核心產品入口（啟動多專家討論）。**刻意不限本機**：對外網站須讓已登入者開討論，否則對外服務癱瘓。安全靠「登入門禁 + 專家 bash 一律 bwrap 沙箱（host 唯讀、PID/網路隔離）」，非以來源限定 |

## 框架/靜態端點（不納管）

`/openapi.json`、`/docs`、`/docs/oauth2-redirect`、`/redoc`、`/static/*`：FastAPI 內建與靜態資源，無狀態變更能力。

## 對應測試

- 寫入納管守門：`tests/test_auth.py::LOOPBACK_WRITE_ENDPOINTS`（公網→403、裸 XFF 偽造→403、來源不可知→403、loopback→放行）。
- 讀取不誤納管守門：`tests/test_auth.py::READ_ENDPOINTS`（結構反查：不含 loopback、仍含 auth）。
- WS：`tests/test_qa_task4_ws_loopback.py`（公網未登入→需登入、公網已登入→進主流程、loopback→進主流程、原始碼不再含 `is_loopback`）。
- 信任模型底層：`tests/test_trust_proxy.py`（XFF 由右往左、受信代理偽造、fail-closed）。
