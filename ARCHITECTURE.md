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
| `routes.py` | REST API（`APIRouter`）：health、登入/登出/狀態、workspace（列檔/讀檔/下載 zip）、history、publish、角色管理（`/api/roles`）、討論小組（`/api/groups`） |
| `ws.py` | WebSocket 端點：啟動 session、串流事件、`_pump_interventions` 收插話/停止 |
| `server.py` | 應用組裝、頁面入口、啟動函式 |
| `orchestrator.py` | `StudioSession`：（選配）需求澄清 → 需求拆解 → 架構辯論 → 逐任務迭代（可並行分波）→ Demo → 驗收/檢討 |
| `discussion.py` | 多角色討論引擎（opt-in，`TI_DISCUSS_MODE`）：N 角色 `round_robin`/`parallel` 兩種發言調度、結構化 `回應 @角色名: 同意\|反對` 引用＋反諂媚硬指令、`flow.is_stalled` 提前收斂、規則式小結；semaphore/broadcast/should_stop 建構時注入，不 import orchestrator |
| `roles.py` | 內建 8 角色定義（CORE 4＋OPTIONAL 4）、共通守則 `_COMMON`；對外接面 `ROSTER`/`BY_KEY` |
| `role_store.py` | 自訂角色檔（`roles/*.md`）載入/驗證/原子落檔＋討論小組（`roles/groups.yaml`）CRUD；「內建為預設、檔案同 key 覆蓋」合併進 `roles` 模組（見〈自訂角色與討論小組〉） |
| `experts.py` | Claude 專家：包裝 `ClaudeSDKClient`，把串流回應轉成事件 |
| `providers.py` | provider 抽象與工廠（Claude / OpenAI 相容） |
| `tools.py` | 非 Claude provider 的 function-calling 工具層（read/write/edit/bash…） |
| `runner.py` | 確定性執行：跑程式/Demo、偵測入口、workspace 內獨立 git；web 服務 HTTP 驗收（`run_http_demo`：啟動服務→輪詢探測→收掉，僅限 localhost；沙箱保留 PID/唯讀隔離、該次共享 loopback） |
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

拆解階段另送一筆 `agenda_plan` 快照（payload：`agenda` 子題列表
〔title/description/criteria/assignee，assignee 已過硬驗證〕、`tasks` 任務清單、
`edges` 依賴邊、`assignments` 分派表〔index 1-based〕、`corrections` 分派修正紀錄
〔index 0-based〕），隨事件流入 `history/<id>.jsonl`，供事後重看與重播。

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
`後續任務:` 自動回填專案 backlog，`核心改動:` 則改路由到 Ti 主核心 repo——見「專案 repo 與
Ti 主核心 repo（雙軌路由）」），或再加 `mode: "improve"` 啟動**持續改良迴圈**
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

### 多角色討論引擎（`discussion.py`，opt-in `TI_DISCUSS_MODE`）

架構討論階段的發言調度。`TI_DISCUSS_MODE=legacy`（預設、非法值 fallback）時 `_debate()`
原始「工程師⇄高級工程師」往返路徑一行不動；`round_robin`／`parallel` 時改走
`DiscussionEngine`：

- **調度**：`round_robin` 同輪內依序發言（後者可見同輪前者）；`parallel` 同輪並行、輪間
  同步——全員基於同一份上一輪 transcript 快照 `asyncio.gather` 發言，收齊才寫回（無輪內
  競態），每次發言包在注入的 `_llm_semaphore()` 下受 `TI_LLM_MAX_CONCURRENCY` 節流。
- **互相回應／反諂媚**：prompt 硬指令要求 `回應 @角色名: 同意|反對 ＋理由` 結構化引用、
  每輪至少指出一個可挑戰點；`parse_mentions()` 以 participants 白名單交替 regex 防禦式
  解析（格式不符整段視為無引用，不錯位）。
- **收斂**：`TI_DISCUSS_MAX_ROUNDS` 硬上限（未設＝`TI_DEBATE_ROUNDS`）＋ `flow.is_stalled`
  相似度提前停止；`stop_reason ∈ {max_rounds, stalled, cancelled}` 落入 `DiscussionResult`。
- **小結**：規則式零 LLM——共識/分歧由 mentions 統計推導、`final_positions` 取各角色末輪
  發言；ADR 開啟時由高工沿用既有蒸餾指令把 final_positions＋末輪發言收斂成決策落盤。
- **依賴方向**：discussion.py 只依賴 stdlib＋`flow.py`＋`config.py`，semaphore／broadcast／
  should_stop 由 orchestrator 建構時注入（嚴禁反向 import，防循環依賴）。

## 自訂角色與討論小組（`role_store.py`、`/api/roles`、`/api/groups`）

內建 8 角色為預設；`roles/*.md` 角色檔同 key 覆蓋內建、新 key 追加。不放任何角色檔時，
`ROSTER`/`BY_KEY` 行為與純內建完全一致。角色檔目錄由 `TI_ROLES_DIR` 指定
（預設 `<專案根>/roles`，可在設定面板「進階」組調整）。

### 角色檔格式（`roles/<key>.md`，Markdown＋YAML frontmatter、一檔一角色）

檔名 stem 即角色 key，須符合 `^[a-z][a-z0-9_]{1,31}$`。範例檔見
`roles/_example.md.sample`（載入器只掃 `*.md`，`.sample` 不會被載入）。

frontmatter 欄位（pydantic 驗證、`extra="forbid"`——未知欄位明確報錯）：

| 欄位 | 型別 | 必填 | 預設 | 說明 |
|------|------|------|------|------|
| `key` | str | 否 | 檔名 stem | 寫了必須與檔名一致，否則拒檔 |
| `name` | str | **是** | — | 中文顯示名，去空白後不可為空 |
| `avatar` | str | 否 | `"🤖"` | emoji |
| `title` | str | 否 | 同 key | 職稱 |
| `model` | str | 否 | `config.MODEL_FAST` | 空字串＝用預設 |
| `allowed_tools` | list[str] | 否 | `["Read", "Grep"]` | 項目不可為空字串 |
| `permission_mode` | str | 否 | `"default"` | 白名單 `{default, acceptEdits}` |
| `tags` | list[str] | 否 | `[]` | 項目不可為空字串 |
| `description` | str | 否 | `""` | 給調度／選人看的一句話描述 |

body 即「角色專屬 system prompt」，載入時自動前置共通守則 `roles._COMMON`，檔案不必
（也不應）手抄共通段。**反空殼 persona（micro-rules）**：body 去空白後須非空，且至少一行
匹配 `(輸出|決議|驗證|格式|指令|決策)[:：]`——確保角色有可解析的出力格式，不收只有形容詞
的空殼。內建 8 角色 body 全數通過此規則（有守門單測），覆蓋內建的「讀出→改→寫回」往返
不會被卡死。

載入/合併規則（`role_store.reload_roles()`）：

- `BY_KEY` ＝ 全部內建（含被 `OPTIONAL_ROLES` 過濾者）＋全部合法檔案角色（BY_KEY ⊇ ROSTER）。
- `ROSTER` ＝ 內建（同 key 檔案覆蓋後、沿用 `OPTIONAL_ROLES` 過濾）＋全部新 key 檔案角色（依 key 排序）。
- 壞檔（缺必填欄位/YAML 壞/未知欄位/空殼 persona）逐檔拒絕並記 log（logger `ti.roles`），不影響其他檔與內建。
- reload 為純同步、一次原地變異（`ROSTER[:]`/`BY_KEY.clear()+update()`/具名常數 setattr），既有 import 綁定保活、無並發空窗。
- **reload 語意**：進行中 session 已快照 Role 物件，reload 只影響之後建立的 expert，不熱換進行中討論。

### `/api/roles`（GET 掛 `require_auth`；POST/PUT/DELETE 掛 `require_admin`＝WRITE_DEPS）

角色回傳形狀 `RoleJson`（GET 列表與寫入成功皆用此形）：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `key` | str | 角色 key |
| `name` | str | 顯示名 |
| `avatar` | str | emoji |
| `title` | str | 職稱 |
| `model` | str | 實際生效模型（預設已展開，非空字串） |
| `allowed_tools` | list[str] | 工具白名單 |
| `permission_mode` | str | `default` / `acceptEdits` |
| `tags` | list[str] | 標籤 |
| `description` | str | 一句話描述 |
| `source` | str | `builtin`（純內建）/ `override`（檔案覆蓋內建）/ `file`（純檔案） |
| `in_roster` | bool | 是否在 ROSTER（被 `OPTIONAL_ROLES` 過濾者為 false） |
| `system_prompt` | str | **角色專屬段原文**（不含 _COMMON 前綴）——可直接改後 PUT 寫回 |

請求體 `RoleBody`（POST 與 PUT 同形；PUT 為**整筆替換**，未給的選填欄位回到預設值）：

| 欄位 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `key` | str | POST 必填；PUT 選填 | 須符合 `^[a-z][a-z0-9_]{1,31}$`；PUT 給了須與路徑一致（不一致 422） |
| `name` | str | 是 | 顯示名 |
| `system_prompt` | str | 是 | 角色專屬段（不含 _COMMON）；須過反空殼 persona 規則，否則 422 |
| `avatar` / `title` / `model` / `allowed_tools` / `permission_mode` / `tags` / `description` | 同角色檔 frontmatter | 否 | 預設值同上表 |

端點與狀態碼（錯誤體為 `{"ok": false, "detail": str}`）：

- `GET /api/roles` → `200 {"roles": [RoleJson]}`（內建＋檔案全部角色）。
- `POST /api/roles`（body=RoleBody）→ `200 {"ok": true, "role": RoleJson}`。
  內建 key＝建立覆蓋檔（允許）；key 已有角色檔回 `409`；key 格式/欄位/persona 驗證失敗回 `422`。
- `PUT /api/roles/{key}`（body=RoleBody）→ `200 {"ok": true, "role": RoleJson}`。
  內建角色＝寫覆蓋檔、檔案角色＝改寫原檔；key 不存在回 `404`；驗證失敗回 `422`。
- `DELETE /api/roles/{key}` → `200 {"ok": true, "restored_builtin": bool}`。
  `file`＝移除自建角色（restored_builtin=false）、`override`＝刪覆蓋檔還原內建（true）；
  純內建回 `409`（不可刪）、不存在回 `404`、key 格式不合法回 `422`。

寫入採 temp＋rename 原子落檔後立即 reload；POST body 的 key 與 PUT/DELETE 路徑參數走同一套
`KEY_RE` 驗證（防路徑穿越）。

### `/api/groups`（全部掛 `require_auth`）

討論小組 `Group = {name: str, role_keys: list[str], mode: str}`，全部小組存單檔
`roles/groups.yaml`（頂層 `groups:` 列表；temp＋rename 原子寫；載入器只掃 `*.md`，
此檔不會被當角色檔）。

寫入時驗證（違反回 `422`，逐條明確訊息）：role_keys 每個 key 必須存在於 `BY_KEY`、
不得重複、成員 ≥2 人；`mode` 白名單 `{round_robin, parallel}`（無 legacy——legacy 是
「無小組」的舊雙人路徑）；`name` 非空、≤64 字元。讀取端不重驗 key 存在性（角色檔可能
事後被刪），讀回忠實呈現檔案內容。

端點與狀態碼（錯誤體為 `{"error": str}`；groups.yaml 損壞回 `500`）：

- `GET /api/groups` → `200 {"groups": [{name, role_keys, mode}]}`（檔案不存在＝空列表）。
- `POST /api/groups`（body：`name: str` 必填、`role_keys: list[str]` 必填、`mode: str` 預設 `"round_robin"`）
  → `200 {"group": {name, role_keys, mode}}`；驗證失敗 `422`；同名已存在 `409`。
- `PUT /api/groups/{name}`（body：`role_keys: list[str]` 與 `mode: str` **皆必填**——整筆替換，
  防漏帶 mode 被預設值默默重置；name 由路徑決定、不可改名）
  → `200 {"group": {...}}`；驗證失敗 `422`；小組不存在 `404`。
- `DELETE /api/groups/{name}` → `200 {"ok": true}`；不存在 `404 {"ok": false}`。

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

## 專案 repo 與 Ti 主核心 repo（雙軌路由）

系統區分兩種 repo 角色，改動依歸屬分流，**互不混合**：

- **專案 repo**：`projects.effective_repo()`＝專案自設 `publish_repo`，否則退回全域 `TI_PUBLISH_REPO`。
  它同時是工作基底（`repo_base.ensure_base` 同步）與發佈目標（`orchestrator._maybe_publish` 對它
  開 PR）。正常的專案改動都進這裡。
- **Ti 主核心 repo**：`config.CORE_REPO`，固定綁定 `AUTOPILOT_REPO`（預設 `x812033727/Ti`），
  即 Ti 框架本身。

**路由規則**：專家若判定「要滿足本需求，必須改動 Ti 核心框架本身
（orchestrator／runner／發佈流程等），而非只改本專案程式碼」，就以結構化行
`核心改動: [P0/bug] <描述>` 輸出（`flow.parse_core_changes` 解析，沿用 `[P0/bug]` 標籤慣例）。
偵測**由專家在討論中判定**——非依檔案路徑、非人工標旗。捕捉點有二：每場討論的**檢討**
階段（`orchestrator._wrap_up` → `result["core_changes"]`），以及持續改良的**「找問題」**階段
（`improver._discover_with_experts`）——兩者都把核心改動與專案任務分流。

這些核心改動**不進專案 backlog、不混入專案 PR**：消費端（`improver`／`ws`／`autopilot`）
一律經單一收斂點 `backlog.route_core_changes(items)` 路由（`source="core"`、省略 `state_dir`＝
核心 backlog `config.AUTOPILOT_STATE_DIR`、並過濾近期已完成的同名項目避免重複/空轉 PR）到
autopilot 在 drain 的那份佇列。autopilot 在
`CORE_REPO` 的 working clone 上實作該改動、過 pytest／lint／no-SDK 閘門與分支保護失效保險，
綠燈才對核心 repo 開**獨立 PR**（分支 `autopilot/task-<id>`，見 `autopilot._commit_push_merge`）。

```
專案 session ──┬─ 一般改動 ─→ 專案 workspace（git 累積）─→ 專案 repo PR（effective_repo）
              └─ 核心改動: ─→ 核心 backlog（AUTOPILOT_STATE_DIR）─→ autopilot 實作
                                                              └─→ x812033727/Ti 獨立 PR
```

設計取捨：核心改動由 autopilot **非同步**產出（需 autopilot 在跑），不在專案 session 內同步完成
——核心改動本就該過完整測試／部署閘門，不該塞進產品 session 趕工。專案 session 只「描述」核心
改動，UI 會出現「核心改動」phase 讓使用者看見路由結果。

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
- `roles/`：自訂角色檔（`<key>.md`）與討論小組設定（`groups.yaml`）；`_example.md.sample` 為範例（不被載入）。

皆可由環境變數覆寫路徑（`TI_WORKSPACE_ROOT` / `TI_HISTORY_ROOT` / `TI_PROJECTS_ROOT` / `TI_ROLES_DIR`）。
