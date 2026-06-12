# 架構說明（Architecture）

Ti Studio 是一個 **FastAPI 後端 + 免建置前端（HTML/CSS/JS）** 的多智能體軟體開發工作室。
本文件說明模組分工、執行期資料流，以及登入門禁的運作方式。

## 高層結構

```
瀏覽器 (web/)  ──HTTP──▶  routes.py   ── REST：health / 登入 / workspace / history / publish
              ──WS────▶  ws.py       ── 即時事件串流 + 人類插話/停止
                              │
                              ▼
                     orchestrator.py  ── StudioSession：工作流程狀態機（核心）
                        │      │     │
            experts/providers  runner  workspace / history / publisher
```

`server.py` 只負責「應用組裝」：建立 `FastAPI` app、掛載 `/static`、`include_router`
（`routes` 與 `ws`）、提供 `/` 與 `/login` 頁面入口，以及 `main()` 啟動 uvicorn。
入口 `studio.server:app` 與 `python -m studio.server` 維持不變。

## 模組職責

| 模組 | 職責 |
|------|------|
| `config.py` | 集中設定：模型、輪數、辯論、Demo、git、門禁、路徑、伺服器；`reload()` 供執行期套用變更 |
| `auth.py` | 單一密碼門禁：cookie token 簽章/驗證、`require_auth` 依賴、WS 檢查 |
| `settings.py` | UI 可調設定（API key / provider / 模型 / GitHub token）：白名單、遮蔽秘密、寫入 .env、`config.reload()` |
| `routes.py` | REST API（`APIRouter`）：health、登入/登出/狀態、workspace（列檔/讀檔/下載 zip）、history、publish |
| `ws.py` | WebSocket 端點：啟動 session、串流事件、`_pump_interventions` 收插話/停止 |
| `server.py` | 應用組裝、頁面入口、啟動函式 |
| `orchestrator.py` | `StudioSession`：（選配）需求澄清 → 需求拆解 → 架構辯論 → 逐任務迭代（可並行分波）→ Demo → 驗收/檢討 |
| `roles.py` | 四位專家的角色定義與 system prompt |
| `experts.py` | Claude 專家：包裝 `ClaudeSDKClient`，把串流回應轉成事件 |
| `providers.py` | provider 抽象與工廠（Claude / OpenAI 相容） |
| `tools.py` | 非 Claude provider 的 function-calling 工具層（read/write/edit/bash…） |
| `runner.py` | 確定性執行：跑程式/Demo、偵測入口、workspace 內獨立 git |
| `workspace.py` | 每個 session 的沙箱工作目錄（安全路徑、列檔、讀檔、打包 zip 匯出） |
| `history.py` | session 事件存檔/讀取（JSONL + meta），供歷史列表與重播；收尾時從事件流推導「成果記分卡」（任務輪數/退回原因/Demo 結果）存進 meta，`/api/metrics` 跨場聚合成功率與近期趨勢 |
| `memory.py` | 任務級反思記憶（per-session JSONL＋fcntl 鎖）：失敗輪蒸餾反思存檔、後續輪 prepend 回 context（opt-in，env `TI_REFLEXION`） |
| `reflexion.py` | 把失敗輪的評審意見蒸餾成文字反思（LLM＋模板 fallback，不裁決成敗、永不崩） |
| `publisher.py` | 把 workspace 成果推成 GitHub 分支並開 PR（預設關閉） |
| `projects.py` | 專案（長期產品）：固定 workspace、專屬 backlog、session 足跡，跨場次累積 |
| `improver.py` | 專案持續改良迴圈：消化 backlog → 跑討論 → followups 回填 → 空了就「找問題」 |
| `blueprint.py` | 產品藍圖（opt-in，`TI_BLUEPRINT`）：improver 開跑時 PM 把願景展開成結構化藍圖（願景/用戶/功能 P0~P2/里程碑），落盤 `BLUEPRINT.md`＋`blueprint.json`、功能餵 backlog、跨場注入 requirement 前綴 |
| `adr.py` | 架構決策記錄（opt-in，`TI_ADR`）：辯論/架構師結論蒸餾成決策條目，落盤 workspace 的 `DECISIONS.md`＋`adr.json`，後續場次注入摘要、翻案須說明理由 |
| `fake_experts.py` | 離線示範用的假專家（真的寫檔/commit，供無金鑰試用與 E2E） |

## 執行期資料流

1. 前端開 WebSocket `/ws`，第一則訊息送 `{requirement}`。
2. `ws.py` 建立 workspace、開始錄製歷史，啟動 `StudioSession.run()`。
3. orchestrator 依階段推進，透過 `broadcast()` 送出 `StudioEvent`（見 `events.py`）：
   `session_started` / `phase_change` / `expert_message` / `tool_use` /
   `board_update` / `run_result` / `git_commit` / `demo_result` / `done` …
4. 每個事件即時送往前端渲染，同時寫入 `history/<id>.jsonl`。
5. 執行中前端可送 `{"type":"interject", text}` 或 `{"type":"stop"}`，由
   `_pump_interventions` 注入 session。
6. 完成後 `done` 事件結束；歷史可從 `/api/history` 列出並重播。

事件型別是前後端的契約，定義集中在 `events.py`，前端在 `web/app.js` 的
`handleEvent()` 對應處理。

## 需求澄清（選配，`TI_CLARIFY`）

開啟後，拆解前 PM 先檢視需求：模糊就向使用者反問最多 `TI_CLARIFY_MAX_QUESTIONS` 個
關鍵問題（每題附「未回覆時的預設假設」），透過 `clarify_request` 事件渲染到前端，
使用者用既有的插話框回答。等待走 1 秒切片輪詢（stop 即時生效），逾時
`TI_CLARIFY_TIMEOUT` 未回覆則按假設續行——**流程絕不因等人而卡死**。
澄清結論前綴進調研／拆解／實作 context，並固化成 workspace 內 `PRD.md`
（追加式；專案模式跨場次累積需求史）。僅互動 session 生效：無插話佇列（autopilot）、
離線 demo、或持續改良迴圈（顯式 `clarify=False`）一律跳過。

## 任務並行（多支線 lane，預設開啟）

並行開啟時（`TI_PARALLEL_TASKS`，預設 `1`；設 `0` 還原純循序），「逐任務迭代」改走**波次排程**：PM 拆解時可宣告
`依賴: #後 -> #前`，`build_waves()` 以拓撲分層把彼此獨立的任務排進同一波次，
波次之間循序（尊重依賴）、波次之內最多 `TI_PARALLEL_LANES` 條支線並行。

每條支線（lane）由 `LaneContext` 隔離：各自一個 **git worktree 分支**
（`runner.git_worktree_add`）與**獨立專家團隊**（避免共用實例的對話累積互相污染），
在自己的工作目錄實作/自測/commit。一波結束時 `_integrate_wave()` 全序列化收尾——
依序把各支線分支合併回主分支（`runner.git_merge_worktree`，衝突則 abort 後於最新主幹
序列化重跑）、把各 lane 的 `NOTES` 緩衝 flush 進共享 `NOTES.md`（單一寫入點、無競態）、
清掉 worktree。並行 lane 的事件會帶 `task_id`，前端據此把多支線發言分色標示。

關閉（預設）時退化成「每任務一波、單一主 lane」，與循序逐任務迭代逐字等價。
全域 `TI_LLM_MAX_CONCURRENCY` 節流同時進行的 LLM 發言數。

## 專案與持續改良迴圈

session 是一次性的；**專案**（`projects.py`）則是「同一個產品做下去」的一級實體：

- `projects/<pid>/meta.json`：名稱、產品願景、歷次 session 足跡。
- `projects/<pid>/backlog.json`：專屬改良任務佇列（`backlog.py` 以 `state_dir` 參數泛化，
  與 autopilot 的全域 backlog 同一套機制、互不干擾）。
- `workspaces/project-<pid>/`：固定 workspace——程式碼與 git 歷史**跨場次累積**，絕不清空。
  刻意放在 `WORKSPACE_ROOT` 下，讓既有檔案/下載 API 與前端檔案面板零改動可用
  （`session_started` 事件帶 `workspace_id`，前端據此對接）。

WebSocket 第一則訊息可帶 `project_id`（在該專案的固定 workspace 上跑單場討論；檢討發現的
`後續任務:` 自動回填專案 backlog），或再加 `mode: "improve"` 啟動**持續改良迴圈**
（`improver.py`，把 autopilot 的自我改善迴圈泛化到任意產品）：

```
取 backlog pending 任務 → 跑一場完整討論（固定 workspace）→ followups 回填 backlog
   ↑                                                            │
   └── backlog 空了：「找問題」（資深專家審視產品、產出新改良任務，已完成標題去重）←┘
```

結束條件：使用者停止／達 `TI_IMPROVE_MAX_CYCLES`／連續失敗達 `TI_IMPROVE_MAX_FAILS`／
「找問題」找不出新改善點（自然收斂）。每一輪（含找問題）各自記錄 history session 可重播；
迴圈結束送出帶 `improve` 摘要的總結 `done` 事件。同一專案同時僅允許一場討論（互斥），
避免固定 workspace 被兩場討論互踩。

### 產品藍圖與優先級（讓「越做越進步」有方向感）

- **藍圖**（`blueprint.py`，opt-in `TI_BLUEPRINT`）：improver 開跑時若專案尚無藍圖，PM 先把
  一句願景展開成結構化藍圖（每專案僅一次）；功能清單按 P0 先餵 backlog（上限
  `TI_BLUEPRINT_SEED_MAX`），之後每輪改良與專案單場討論注入藍圖前綴。解析不出結構時降級
  存原文、不擋迴圈。
- **backlog 優先級**：任務帶 `priority`（P0~P2）/`type`（feature/bug/improvement）欄位，
  `next_pending` 按「priority 小者先、同級內先進先出」出列；舊資料無欄位視為 P1，順序與
  先前 FIFO 一致（零遷移）。「找問題」與檢討 `後續任務:` 支援可選 `[P0/bug]` 標籤
  （`parse_structured_tasks`／`parse_followups_meta`，標籤缺省/寫壞退回預設）。
- **ADR**（`adr.py`，opt-in `TI_ADR`）：架構師定案（或無架構師時由高工把辯論共識收斂成
  `決策:/理由:/否決:` 行）後落盤 `DECISIONS.md`＋`adr.json`；下一場同 workspace 的 PM 拆解
  與架構提案注入既有決策摘要——翻案須說明理由，避免跨場次反覆推翻。
- 前端「📦 專案」面板顯示藍圖卡片與按消化順序排序的 backlog（priority/type 徽章）。

## 認證 / 門禁流程

門禁由 `TI_ACCESS_PASSWORD` 控制，**預設停用**（向後相容）。

- **停用時**：`auth.is_authed()` 恆為 True，所有路由與 WS 照常放行。
- **啟用時**：
  1. `GET /` 未登入 → 回 `web/login.html`（登入頁）。
  2. `POST /api/login`（密碼正確）→ 以 `hmac` 簽章產生帶時間戳的 token，寫入
     `httponly` cookie（`ti_session`，預設 7 天）。
  3. 受保護的 REST 路由掛 `Depends(auth.require_auth)`，未帶有效 cookie 回 401。
  4. WebSocket 握手後檢查 cookie，未登入送 error 並 `close(1008)`。
  5. `POST /api/logout` 清除 cookie；前端右上角「登出」按鈕觸發。

token 以標準庫 `hmac`（SHA-256）簽章，不引入額外依賴；密鑰為 `TI_AUTH_SECRET`
（留空則每次啟動隨機產生，重啟即讓所有登入失效）。

## 設定流程（UI 設定 API key / provider / 模型 / GitHub）

- `GET /api/settings`：回傳 `settings.FIELDS` 的目前狀態；秘密欄位**不含明文**，只回報 `set`。
- `POST /api/settings`：只接受白名單（`settings.ALLOWED`）內的鍵；秘密欄位留空＝不變更、
  select 欄位驗證選項。寫入專案根目錄 `.env` 經 `secretfile.write_secret_file`（安全寫法：
  與 umask 脫鉤、保證檔案 0600、收緊既存寬鬆權限）並更新 `os.environ`，
  最後呼叫 `config.reload()` 重新載入可調設定，**下次討論即生效，無需重啟**。
  存取密碼（`auth.set_password`）亦走同一安全寫入路徑。
- Claude 模型選擇靠 `experts._model_for(role)` 在每個 session 建立專家時即時讀取 `config`。

## 指定 GitHub repo（在現有專案上工作）

- WebSocket 第一則訊息除了 `requirement`，可附 `repo_url`（與選用的 `repo_branch`）。
- `runner.is_valid_repo_url` 僅放行 github.com 的 https 網址；`runner.git_clone` 在啟動討論前
  把 repo `clone` 進該 session 的 workspace（私有 repo 會以 `GITHUB_TOKEN` 注入認證，且輸出/
  指令會遮蔽 token）。`StudioSession(repo_url=...)` 會讓 PM 先閱讀現有結構再拆解任務。
- 離線示範模式會忽略 `repo_url`（假專家自行寫檔，避免衝突）。

## 成果匯出下載

- `GET /api/workspace/{session_id}/download`（掛 `Depends(auth.require_auth)`）：呼叫
  `workspace.zip_workspace()` 把該 session 的 workspace 即時打包成記憶體 zip，回 `Response`
  （`application/zip` + `Content-Disposition: attachment`）。找不到 workspace 或無產出時回 404。
- 打包內容沿用 `workspace.list_files()`，因此自動排除 `.git` / `__pycache__` / `node_modules` 等
  雜訊；所有寫入路徑都在 `workspace_path()` 沙箱內，路徑穿越的 `session_id` 會被字元過濾擋下。
- 前端「產出檔案」面板的「⬇️ 下載成果」按鈕（有產出時才顯示）以隱藏連結觸發瀏覽器下載，
  同源 cookie 自動帶上（門禁啟用時）。

## 前端（web/）

免建置、無框架：

- `index.html` / `app.js` / `styles.css`：工作室主頁（討論串、看板、檔案、歷史、重播、設定面板、
  GitHub repo 網址輸入）。`app.js` 載入時先打 `/api/auth/status`，門禁啟用且未登入則導向 `/login`。
- `login.html` / `login.js`：登入頁，送 `/api/login` 後導回 `/`。

## 資料夾

- `workspaces/<session_id>/`：每個 session 的產出與獨立 git repo。
- `workspaces/project-<pid>/`：專案的固定 workspace（跨場次累積，不被 history 回收）。
- `history/<session_id>.jsonl` + `.meta.json`：事件存檔與摘要。
- `projects/<pid>/`：專案 meta 與專屬 backlog。

皆可由環境變數覆寫路徑（`TI_WORKSPACE_ROOT` / `TI_HISTORY_ROOT` / `TI_PROJECTS_ROOT`）。
