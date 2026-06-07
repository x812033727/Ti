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
| `routes.py` | REST API（`APIRouter`）：health、登入/登出/狀態、workspace、history、publish |
| `ws.py` | WebSocket 端點：啟動 session、串流事件、`_pump_interventions` 收插話/停止 |
| `server.py` | 應用組裝、頁面入口、啟動函式 |
| `orchestrator.py` | `StudioSession`：需求拆解 → 架構辯論 → 逐任務迭代 → Demo → 驗收/檢討 |
| `roles.py` | 四位專家的角色定義與 system prompt |
| `experts.py` | Claude 專家：包裝 `ClaudeSDKClient`，把串流回應轉成事件 |
| `providers.py` | provider 抽象與工廠（Claude / OpenAI 相容） |
| `tools.py` | 非 Claude provider 的 function-calling 工具層（read/write/edit/bash…） |
| `runner.py` | 確定性執行：跑程式/Demo、偵測入口、workspace 內獨立 git |
| `workspace.py` | 每個 session 的沙箱工作目錄（安全路徑、列檔、讀檔、打包 zip 匯出） |
| `history.py` | session 事件存檔/讀取（JSONL + meta），供歷史列表與重播 |
| `publisher.py` | 把 workspace 成果推成 GitHub 分支並開 PR（預設關閉） |
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
  select 欄位驗證選項。寫入專案根目錄 `.env`（`dotenv.set_key`）並更新 `os.environ`，
  最後呼叫 `config.reload()` 重新載入可調設定，**下次討論即生效，無需重啟**。
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
- `history/<session_id>.jsonl` + `.meta.json`：事件存檔與摘要。

兩者皆可由環境變數覆寫路徑（`TI_WORKSPACE_ROOT` / `TI_HISTORY_ROOT`）。
