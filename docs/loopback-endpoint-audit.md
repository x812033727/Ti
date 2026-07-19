# 端點盤點：管理寫入門禁（require_admin）納管清單

> 盤點 `studio/routes.py`（HTTP）與 `studio/ws.py`（WebSocket）所有入口，
> 標記「掛管理門禁」的端點與理由。事實依據以 `app.routes` 反查（含實際生效的 dependant），
> 非靠人工讀碼，避免漏列。

## 判定原則

- **納管（掛 `require_admin`）**：會改機器狀態／控制面／可外洩秘密的**寫入**端點。
  `require_admin` 是 fail-safe 複合門禁：**門禁啟用**（設了 `TI_ACCESS_PASSWORD`）時等同
  `require_auth`——外網已登入者可操作（重新部署/改設定/控 autopilot），未登入回 401；
  **門禁停用**時退回 `require_loopback` 僅限本機（403），不把控制面裸露給全網
  （`HOST` 預設 `0.0.0.0`，且 `is_authed` 在門禁停用時恆放行）。
- **不納管**：讀取查詢、認證握手、靜態與框架端點。
- fail-safe 分支的信任判定統一走 `netutil.is_loopback`（spoof-safe、fail-closed），
  HTTP 用 `WRITE_DEPS` 依賴掛 `require_admin`。
- 回應碼：門禁啟用未登入 → HTTP 401「需要登入」；門禁停用非本機 → HTTP 403「僅限本機存取」。

## HTTP 端點

| 方法 | 路徑 | 現況 deps | 納管 | 理由 |
|------|------|-----------|:----:|------|
| POST | `/api/redeploy` | admin（auth｜fail-safe loopback） | ✅ | 拉 main 並自我重啟，高危機器狀態變更 |
| POST | `/api/claude-account/switch` | admin（auth｜fail-safe loopback） | ✅ | 切換 Claude 在線訂閱帳號（換憑證檔 + 重啟 ti.service/ti-autopilot）＝進入手動模式（釘選），高危服務狀態變更；有討論/任務進行中時預設回 409 擋下，UI 提供二選一：`queue=true`＝排空後切換（寫 pin 檔、由 autopilot 於討論空檔代切，回 202，零損失）;`force=true`＝強制切換（立即中斷討論、優雅停機退回任務） |
| DELETE | `/api/claude-account/pin` | admin（auth｜fail-safe loopback） | ✅ | 解除帳號釘選＝恢復自動輪替（刪 pin 哨兵檔），影響後續帳號分配決策的服務狀態變更 |
| POST | `/api/publish/{session_id}` | admin（auth｜fail-safe loopback） | ✅ | 觸發對外發佈（push＋開 PR＋等 CI 合併）的對外狀態變更；#196 起由 `auth` 升級為 `WRITE_DEPS`，與其他寫入端點同級 |
| POST | `/api/auth/password` | admin（auth｜fail-safe loopback） | ✅ | 寫 .env 改存取密碼，秘密寫入面；門禁停用時限本機，公網裸部署不致被搶先設密碼接管 |
| POST | `/api/settings` | admin（auth｜fail-safe loopback） | ✅ | 改 .env 設定（含 `OPENAI_BASE_URL` 等），可致金鑰外洩/RCE 風險 |
| POST | `/api/autopilot/pause` | admin（auth｜fail-safe loopback） | ✅ | 控制自動迴圈，遠端可癱瘓（DoS） |
| POST | `/api/autopilot/resume` | admin（auth｜fail-safe loopback） | ✅ | 控制自動迴圈狀態 |
| POST | `/api/autopilot/dispatch-mode` | admin（auth｜fail-safe loopback） | ✅ | 切換派工模式哨兵檔（auto＝PM 全權派工/manual），影響後續 session 的 provider/模型分配 |
| POST | `/api/autopilot/task` | admin（auth｜fail-safe loopback） | ✅ | 向會自主執行 bash 的 autopilot 注入任務 |
| POST | `/api/autopilot/task/{task_id}/action` | admin（auth｜fail-safe loopback） | ✅ | 看板手動操作單一任務（retry/park/unpark/priority），改寫 backlog 狀態 |
| POST | `/api/autopilot/triage` | admin（auth｜fail-safe loopback） | ✅ | 分診 failed 任務（基礎設施型退回 pending 重試／陳年失敗歸檔 parked），改寫 backlog 狀態 |
| POST | `/api/notify/test` | admin（auth｜fail-safe loopback） | ✅ | 發送測試推播（webhook/Telegram）＝觸發對外網路呼叫且間接證實已設憑證，管理面操作 |
| POST | `/api/roles` | admin（auth｜fail-safe loopback） | ✅ | 寫入角色檔 `roles/<key>.md` 並 reload 角色表（system_prompt 注入面） |
| PUT | `/api/roles/{key}` | admin（auth｜fail-safe loopback） | ✅ | 改寫角色檔並 reload（同 POST 寫入面） |
| DELETE | `/api/roles/{key}` | admin（auth｜fail-safe loopback） | ✅ | 刪角色檔（file＝移除、override＝還原內建），機器狀態變更 |
| POST | `/api/groups` | admin（auth｜fail-safe loopback） | ✅ | 寫入討論小組設定 `roles/groups.yaml`（組隊/mode 注入面），#120 起與 `/api/roles` 同級保護 |
| PUT | `/api/groups/{name}` | admin（auth｜fail-safe loopback） | ✅ | 改寫討論小組設定（同 POST 寫入面） |
| DELETE | `/api/groups/{name}` | admin（auth｜fail-safe loopback） | ✅ | 刪除討論小組條目，設定面狀態變更 |
| POST | `/api/workflows` | admin（auth｜fail-safe loopback） | ✅ | 寫入動態流程設定 `roles/workflows.yaml`（stage 序列/角色/閘門注入面），與 `/api/groups` 同級保護 |
| PUT | `/api/workflows/{name}` | admin（auth｜fail-safe loopback） | ✅ | 改寫動態流程設定（同 POST 寫入面） |
| DELETE | `/api/workflows/{name}` | admin（auth｜fail-safe loopback） | ✅ | 刪除動態流程條目，設定面狀態變更 |
| GET  | `/api/roles` | auth | ➖ | 讀取角色表（內建＋檔案，含來源標記），無秘密 |
| GET  | `/api/settings` | auth | ➖ | 讀取設定（秘密欄位不回明文），敏感度低 |
| GET  | `/api/provider-quota` | auth | ➖ | 讀取 provider ready/auth 狀態、可列模型與 Ti 本機用量彙總；不回傳 API key/OAuth token |
| GET  | `/api/autopilot` | auth | ➖ | 讀取狀態 |
| GET  | `/api/autopilot/backlog` | auth | ➖ | 讀取待辦清單 |
| GET  | `/api/autopilot/activity` | auth | ➖ | 讀取任務動態視圖（backlog × history 記分卡/token 用量聚合） |
| GET  | `/api/autopilot/audit-trend` | auth | ➖ | 唯讀：audit.jsonl 每日 outcome 分佈與完成率趨勢 |
| GET  | `/api/autopilot/trust` | auth | ➖ | 唯讀：信任指標（零人工介入合併率/介入分類/系統事件計數，第 3 階 A0） |
| GET  | `/api/autopilot/investigations` | auth | ➖ | 唯讀：調查任務結論清單（backlog note＋audit join） |
| GET  | `/api/lessons` | auth | ➖ | 唯讀：教訓庫瀏覽（子字串搜尋） |
| GET  | `/api/autopilot/digest` | auth | ➖ | 唯讀：週報 digest（audit/backlog/lessons 純模板彙整） |
| GET  | `/api/autopilot/digests` | auth | ➖ | 唯讀：已落盤 digest 歷史清單（每日排程寫檔） |
| GET  | `/api/autopilot/digests/{name}` | auth | ➖ | 唯讀：單一落盤 digest 內容（檔名白名單擋穿越） |
| GET  | `/api/history` | auth | ➖ | 讀取歷史列表 |
| GET  | `/api/history/{session_id}/events` | auth | ➖ | 讀取單場事件 |
| GET  | `/api/workspace/{session_id}/files` | auth | ➖ | 讀取工作區檔案清單 |
| GET  | `/api/workspace/{session_id}/file` | auth | ➖ | 讀取單一檔案內容 |
| GET  | `/api/workspace/{session_id}/download` | auth | ➖ | 下載工作區壓縮檔 |
| GET  | `/api/publish/config` | auth | ➖ | 讀取發佈設定 |
| GET  | `/api/groups` | auth | ➖ | 讀取討論小組清單（roles/groups.yaml） |
| GET  | `/api/workflows` | auth | ➖ | 讀取動態流程清單（roles/workflows.yaml＋內建預設） |
| GET  | `/api/projects` | auth | ➖ | 讀取專案列表與 backlog 統計 |
| GET  | `/api/projects/{project_id}` | auth | ➖ | 讀取單一專案 meta 與 backlog |
| GET  | `/api/metrics` | auth | ➖ | 讀取運維指標（活躍場次/並發上限/history 計數/保留策略/workspace 數），無秘密 |
| GET  | `/api/appraisals` | auth | ➖ | 讀取 AI 成員考核聚合（per provider 平均分/樣本數/通過率）與最近紀錄，無秘密 |
| POST | `/api/login` | — | ➖ | 認證握手，必須對外可達才能登入 |
| POST | `/api/logout` | — | ➖ | 認證握手 |
| GET  | `/api/auth/status` | — | ➖ | 公開狀態查詢（前端判斷是否需登入） |
| GET  | `/api/health` | — | ➖ | 健康檢查，需對外可達 |
| GET  | `/`, `/login` | — | ➖ | 前端 HTML 入口 |

### 寫入但**刻意不納管**（須記錄理由，供追溯）

| 方法 | 路徑 | 現況 deps | 理由 |
|------|------|-----------|------|
| DELETE | `/api/history/{session_id}` | auth | 刪除歷史紀錄，作用於資料而非機器控制面；門禁已足夠 |
| POST | `/api/history/cleanup/completed` | auth | 清理已完成歷史，同上 |
| POST | `/api/history/cleanup/retention` | auth | 依保留策略回收超量/過舊歷史，作用於資料面而非機器控制面；同上 |
| POST | `/api/projects` | auth | 建立專案（寫 meta 與空 workspace 目錄），純資料面；與 /ws 同屬核心產品操作，須對已登入外網使用者可用 |
| POST | `/api/projects/{project_id}/backlog` | auth | 往「專案」backlog 排改良任務。與 autopilot 的任務注入端點（納管）不同：專案任務僅在已登入使用者經 /ws 主動啟動持續改良時才執行，且專家 bash 走 bwrap 沙箱（與 /ws 同安全模型），非無人值守自動執行 |
| POST | `/api/projects/{project_id}/recover` | auth | 中斷恢復：把卡在 in_progress 的 backlog 任務重置回 pending、幽靈 running meta 標 error，皆冪等且僅作用於該專案的資料面；迴圈進行中回 409 防競爭，不啟動任何執行（重啟由前端走 /ws 既有流程） |
| DELETE | `/api/projects/{project_id}` | auth | 刪除專案（meta/backlog/藍圖/固定 workspace），作用於資料面而非機器控制面，與 DELETE history 同級；進行中回 409 防止對著被抽掉的目錄繼續寫檔；history 紀錄保留 |
| POST | `/api/sessions/{target_id}/stop` | auth | 對進行中討論／改良迴圈送停止指令（與 /ws 的 stop 同一條 request_stop 管線，僅作用於使用者自己啟動的場次）；讓斷線後背景續跑的討論也停得掉，與 /ws 同安全模型 |
| POST | `/api/projects/{project_id}/publish-repo` | auth | 設定專案自己的發佈 repo（owner/repo，留空清除）：純 meta 寫入（格式白名單驗證），實際對外推送仍由 session 結束的既有發佈流程執行，token 不經此端點 |

> 註：上列皆屬「資料面寫入」，架構決策的納管邊界限定在「會改機器狀態的控制面/秘密寫入」。
> 若後續威脅模型升級，可比照 `WRITE_DEPS` 一行掛上，已有守門測試結構可直接擴充。

## WebSocket 端點

| 端點 | 檢查方式 | 納管 | 理由 |
|------|----------|:----:|------|
| `/ws` | handler 內 `auth.is_authed`（共用密碼門禁） | ❌ | 核心產品入口（啟動多專家討論）。**刻意不限本機**：對外網站須讓已登入者開討論，否則對外服務癱瘓。安全靠「登入門禁 + 專家 bash 一律 bwrap 沙箱（host 唯讀、PID/網路隔離）」，非以來源限定。HTTP 管理寫入現同此模型，另加門禁停用時的 fail-safe 本機限定（見上） |

## 框架/靜態端點（不納管）

`/openapi.json`、`/docs`、`/docs/oauth2-redirect`、`/redoc`、`/static/*`：FastAPI 內建與靜態資源，無狀態變更能力。

## 對應測試

- 寫入納管守門：`tests/server/test_auth.py::ADMIN_WRITE_ENDPOINTS`（門禁啟用：公網未登入→401；
  門禁停用 fail-safe：公網→403、裸 XFF 偽造→403、來源不可知→403、loopback→放行）。
- 已登入放行與 fail-safe 兩面：`tests/test_qa_admin_gate.py`（公網已登入→放行全部 6 端點）。
- 讀取不誤納管守門：`tests/server/test_auth.py::READ_ENDPOINTS`（結構反查：不含 loopback/admin、仍含 auth）。
- WS：`tests/test_qa_task4_ws_loopback.py`（公網未登入→需登入、公網已登入→進主流程、loopback→進主流程、原始碼不再含 `is_loopback`）。
- 信任模型底層：`tests/test_trust_proxy.py`（XFF 由右往左、受信代理偽造、fail-closed）。
