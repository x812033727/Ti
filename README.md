# Ti Studio — AI 專家討論工作室

[![CI](https://github.com/x812033727/Ti/actions/workflows/ci.yml/badge.svg)](https://github.com/x812033727/Ti/actions/workflows/ci.yml)

一個由多位 AI 專家組成的自主軟體開發「工作室」。給它一段產品需求，工作室裡的
**專案經理、工程師、高級工程師、驗證工程師** 就會自己討論、寫程式、測試、審查、
反覆改進，最後做出可運行的成果 —— 整個過程會在網頁上即時呈現。

預設由 [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) 驅動，
專家們使用內建的 Read / Write / Edit / Bash 工具真的去寫檔案、執行程式。
也可切換到 **OpenAI 或本地相容模型**（透過 function-calling 工具迴圈，同樣能自己 coding）。

## 工作流程

```
①需求拆解   PM 拆成結構化任務 + 驗收標準 + 執行指令
②架構辯論   工程師 ⇄ 高級工程師 來回討論整體做法
③逐任務迭代  for 每個任務（看板 todo→doing→review→done）：
              工程師實作（交付前自測）→ smoke-run + git commit
              → 驗證工程師測試 → 高級工程師審查（帶入測試 log）
              → 通過？否則把【測試+審查意見】原文回饋，重跑（每任務最多 3 輪）
④最終 Demo   實際執行整體產出，顯示 stdout/stderr
⑤驗收+檢討   PM 判定完成 → 團隊回顧 → 完成
```

- **人類可中途插話**：執行中於插話框輸入指示，專家會在下一步納入考量；亦可隨時「停止」。
- **階段性 git**：每輪在 workspace 內的獨立 repo 自動 commit，留下可追蹤歷史。
- **詳細 log**：自測與 Demo 的完整輸出都會回報到討論串（可展開查看）。
- **歷史存檔/重播**：每次 session 的事件自動存檔，可從「📜 歷史」面板挑選並重播當時的討論過程。
- **成果發佈到 GitHub**：設定 token 與目標 repo 後，可手動（或自動）把 workspace 成果推成分支並開 PR。
- **成果匯出下載**：產出檔案面板的「⬇️ 下載成果」按鈕會把該 session 的 workspace 打包成 zip 下載（自動排除 `.git/` 等雜訊）。

## 角色

| 角色 | 職責 | 工具 |
|------|------|------|
| 🧭 專案經理 | 拆解需求、定驗收標準與執行指令、判斷完成、主持檢討 | 唯讀 |
| 👩‍💻 工程師 | 實際撰寫/修改程式碼、交付前自測、依意見修正 | Read/Write/Edit/Bash |
| 🔬 驗證工程師 | 撰寫並執行測試、回報 pass/fail 與 log | Read/Write/Edit/Bash |
| 🧠 高級工程師 | 參與架構辯論、審查品質/設計/安全、核可或退回 | 唯讀 + Bash |

## 安裝

需要 Python 3.10+ 與 [Claude Code](https://code.claude.com) 執行環境。

```bash
pip install -e .            # 或：pip install claude-agent-sdk fastapi "uvicorn[standard]" python-dotenv
cp .env.example .env        # 填入 ANTHROPIC_API_KEY
```

## 啟動

```bash
export ANTHROPIC_API_KEY=sk-...      # 或寫在 .env
python -m studio.server              # 或：uvicorn studio.server:app
```

開啟瀏覽器 http://localhost:8000 ，輸入需求（例如「做一個能計算 BMI 並分類的 Python CLI」），
按「開始討論」即可觀看專家協作。產出的程式碼會放在 `workspaces/<session_id>/`。

討論產出檔案後，右側「產出檔案」面板會出現「⬇️ 下載成果」按鈕，點擊即可把整個 workspace
打包成 `workspace-<session_id>.zip` 下載到本機（自動排除 `.git/` 等雜訊；門禁啟用時需先登入）。

### 登入 / 門禁（選填）

預設不需登入。若要讓工作室只開放給知道密碼的人，設定一組共用密碼即可：

```bash
TI_ACCESS_PASSWORD=你的密碼 python -m studio.server
```

啟用後，未登入者會被導向登入頁，所有 API 與 WebSocket 都需登入才能使用；右上角會出現
「登出」按鈕。登入狀態以簽章 cookie 維持（預設 7 天，見 `TI_AUTH_TTL`）。
未設定 `TI_ACCESS_PASSWORD` 時門禁完全停用，本地開發與離線示範不受影響。

### ⚙️ 設定頁（API key / provider / 模型 / GitHub token）

不想改環境變數也可以直接在網頁上設定：按右上角「⚙️ 設定」，即可填入

- 後端 provider（claude / openai）
- Claude API key、Claude 主力 / 快速模型
- OpenAI API key、Base URL（可指向本地模型）、OpenAI 模型
- GitHub token、發佈目標 repo

儲存後會寫入伺服器的 `.env` 檔（已被 git 忽略），並**於下次討論即時生效，無需重啟**。
秘密欄位（key / token）在頁面上不會回顯明文，留空代表「不變更」。

設定面板底部還有「**存取密碼（登入門禁）**」區塊,可直接變更登入密碼:
門禁已啟用時需先輸入目前密碼;門禁未啟用時則可在此設定一組密碼以首次啟用。變更會寫入
`.env`(`TI_ACCESS_PASSWORD`)並即時生效;既有登入不會因此被登出(cookie 以 `TI_AUTH_SECRET`
簽章,與密碼無關)。

### 在現有的 GitHub 專案上工作

想讓專家討論 / 修改一個現有專案，而不是從零開始：在頂部「GitHub repo 網址」欄位填入
倉庫網址（例如 `https://github.com/owner/repo`），按「開始討論」即可。系統會先把該 repo
**clone 進這次的 workspace**，PM 會先閱讀現有結構再拆解任務。私有倉庫請先在設定頁填入
GitHub token。（僅支援 github.com 的 https 網址；離線示範模式會忽略此欄位。）

### 離線示範模式（不需 API 金鑰）

想先試用整套流程、或在沒有金鑰的環境驗證，可開啟離線模式：用腳本化的假專家驅動
**真實的** 流程（真的寫檔、git commit、執行 Demo）。

```bash
TI_OFFLINE=1 python -m studio.server
```

輸入任意需求即可看到完整流程：PM 把工作拆成 **3 個任務**，工程師逐任務寫出一個真實的小專案
（`calculator.py` / `main.py` / `README.md`），驗證工程師補上 `test_calculator.py`，看板隨任務
移動、每輪自動 git commit，最後 **Demo 真的執行 `python main.py add 3 4` 算出 `7.0`**。

## 設定

可用環境變數（見 `.env.example`）調整：

<!-- 維護注意：勿在此環境變數表「之前」出現 TI_AUTOPILOT_* 完整變數名。
     tests/test_qa_task6_docs.py 以 next() 取首個含該變數名的行做同行斷言，
     表格行必須是檔案中首個含完整變數名之處，否則測試會誤判預設值。
     旗標的風險／前提／解析規則一律寫在表格「下方」的補充區塊，並以簡稱（反引號）引用。 -->

| 變數 | 說明 | 預設 |
|------|------|------|
| `TI_MODEL_LEAD` / `TI_MODEL_FAST` | PM/高級工程師 與 工程師/QA 使用的模型 | opus / sonnet |
| `TI_MAX_ROUNDS` | 每個任務的最大改進輪數 | 3 |
| `TI_DEBATE_ROUNDS` | 架構辯論來回回合數（0 = 關閉） | 2 |
| `TI_DEMO_TIMEOUT` / `TI_DEMO_MAX_OUTPUT` | 自測/Demo 的逾時秒數與輸出字數上限 | 60 / 8000 |
| `TI_ENABLE_GIT` | 是否在 workspace 內做階段性 commit | 1 |
| `TI_HOST` / `TI_PORT` | 伺服器位址 | 0.0.0.0 / 8000 |
| `TI_ACCESS_PASSWORD` | 設定後啟用登入門禁（共用密碼） | 未設定（停用） |
| `TI_AUTH_SECRET` / `TI_AUTH_TTL` | cookie 簽章密鑰 / 登入有效秒數 | 隨機 / 604800 |
| `GITHUB_TOKEN` + `TI_PUBLISH_REPO` | 設定後啟用「發佈成果到 GitHub」（owner/repo） | 未設定 |
| `TI_PUBLISH_BASE` / `TI_PUBLISH_AUTO` | PR 目標分支 / 完成後是否自動發佈 | main / 0 |
| `TI_PUBLISH_MERGE` | push／開 PR 後是否自動合併（先等 CI 通過才合併） | 0 |
| `TI_PUBLISH_CI_TIMEOUT` / `TI_PUBLISH_CI_INTERVAL` | 自動合併前等待 CI 的最長秒數 / 輪詢間隔 | 600 / 10 |
| `TI_PUBLISH_MERGE_RETRIES` | 對 stale／`Base branch was modified`（409）的重試次數 | 3 |
| `TI_OFFLINE` / `TI_OFFLINE_DELAY` | 離線示範模式（不需金鑰）/ 發言節奏秒數 | 0 / 0.4 |
| `TI_PROVIDER` | 後端 provider：`claude` 或 `openai` | claude |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 金鑰 / 相容端點（可指向本地模型） | 未設定 |
| `TI_OPENAI_MODEL_LEAD` / `TI_OPENAI_MODEL_FAST` | OpenAI 主力 / 快速模型 | gpt-4o / gpt-4o-mini |
| `TI_AUTOPILOT_FORCE_PUSH` | Autopilot 推送策略：預設非強制（`git push`），遠端已存在同名分支時中止；設 `1` 才略過中止並改用 `--force-with-lease --force-if-includes` 覆寫殘留分支（絕不用裸 `-f`） | 0（安全側） |
| `TI_AUTOPILOT_MERGE_ADMIN` | Autopilot 合併策略：預設不帶 `gh pr merge --admin`，讓 GitHub 分支保護生效；目標 branch 有保護規則且需維持自動合併時設 `1` | 0（安全側） |
| `TI_AUTOPILOT_PROTECTION_CHECK` | 第二道防線：squash-merge 前主動查「合併目標分支（`TI_AUTOPILOT_BRANCH`，預設 `main`）」的保護狀態。優先打 Rulesets 端點（classic token 即可讀、**多半不需 `Administration:read`**），舊 branch protection 端點為輔。三態 fail-safe——受保護/無保護皆放行，唯「無法確認」（403 無權／網路／逾時）一律**中止**並回含「無法確認保護狀態」字樣的訊息，絕不誤判為無保護而放行。讀舊 protection 端點才需 `Administration:read`；無此權限而持續卡「無法確認」的環境，設 `0` 整段跳過（明確逃生口） | 1（啟用） |

#### Autopilot 安全旗標補充

上表兩個旗標預設皆為安全側（`0`），啟用前請先確認已設好分支保護與 CI gating。

- **`FORCE_PUSH` 風險**：開啟後，遠端已存在同名分支時不再中止，改以
  `git push --force-with-lease --force-if-includes` 覆寫。若該分支上有他人 commit，**會被直接覆蓋**；
  且 `--force-with-lease` 在背景 `git fetch`（例如 cron）默默更新本地 ref 後可能失效，安全性退化為形同裸 force。
  事後救援僅能靠**本機 reflog**，已 push 出去而隊友端沒有的 commit 無法復原。故僅建議用於覆寫 autopilot 自己殘留的分支。
- **`MERGE_ADMIN` 前提**：帶 `gh pr merge --admin` 以管理員權限立即合併、繞過分支保護，因此**呼叫者本身需具該 repo 的 admin 權限**，否則指令會失敗。
  另注意若該 repo 採用較新的 **Rulesets**（而非 classic branch protection），`--admin` 可能**無法繞過**「至少一個 approval」等規則而仍被擋下；
  此時需改走 `gh api .../pulls/{n}/merge -X PUT` 之類的 workaround。設 `1` 不代表保證能自動合併，實際以該 repo 的權限與保護設定為準。
- **解析規則**：兩旗標只有 `0`、`false`、`False`、空值、未設定這五種會判為「關閉」，**其餘任何值一律視為開啟**。
  此比對是**字面完全相符、區分大小寫**（程式為 `not in ("0","false","False","")`，無 `.lower()`），
  所以 `FALSE`（全大寫）、`no`、`off`、`disable` 等都**不在關閉集合內，會被當成開啟**。要關閉請固定填 `0`。

### 切換到 OpenAI / 本地模型

```bash
pip install -e ".[openai]"
TI_PROVIDER=openai OPENAI_API_KEY=sk-xxx python -m studio.server
# 本地模型（OpenAI 相容，如 Ollama）：
TI_PROVIDER=openai OPENAI_BASE_URL=http://localhost:11434/v1 TI_OPENAI_MODEL_LEAD=llama3.1 python -m studio.server
```

## 測試

不需 API 金鑰的流程狀態機單元測試（指令以 Linux/macOS 為準；假設已依 [CONTRIBUTING.md](CONTRIBUTING.md) 建好 `.venv`。Windows 請改用 `.venv\Scripts\python`）：

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest                 # 跑測試
ruff check . && ruff format --check .   # 跑 lint / 格式檢查
pre-commit install     # （選填）裝 git hook，提交前自動 lint
```

開發流程、分支與提交慣例見 [CONTRIBUTING.md](CONTRIBUTING.md)；
模組與資料流的完整說明見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 架構

```
studio/
  config.py        設定（模型、輪數、辯論、Demo、git、門禁、伺服器）+ 執行期 reload
  auth.py          單一密碼門禁：cookie 簽章/驗證、FastAPI 依賴與 WS 檢查
  settings.py      可由 UI 調整的設定（API key / provider / 模型 / GitHub），持久化到 .env
  roles.py         四位專家的角色與 system prompt
  events.py        StudioEvent 事件（WebSocket 傳輸）
  workspace.py     每個 session 的沙箱工作目錄
  experts.py       Claude 專家：包裝 ClaudeSDKClient，串流回應轉事件
  providers.py     provider 抽象與工廠（Claude / OpenAI 相容）
  tools.py         非 Claude provider 的工具層（read/write/edit/bash function-calling）
  orchestrator.py  StudioSession：逐任務工作流程狀態機（核心）
  runner.py        確定性執行：跑程式/Demo、偵測入口、workspace 內獨立 git
  history.py       session 事件存檔/讀取（供歷史列表與重播）
  publisher.py     把 workspace 成果推成 GitHub 分支並開 PR（預設關閉）
  fake_experts.py  離線示範用的假專家（真的寫檔，供無金鑰試用/端到端驗證）
  routes.py        REST API 路由（health / 登入 / workspace / history / publish）
  ws.py            WebSocket 端點（即時串流 + 人類插話/停止）
  server.py        應用組裝：建立 FastAPI app、掛載靜態檔與路由、頁面入口
web/               免建置的工作室前端（HTML/CSS/JS；含登入頁、響應式版面與重播）
tests/             單元測試 + 離線端到端測試（test_offline_e2e.py、test_auth.py）
```

更完整的模組地圖與資料流請見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 後續可擴充

產出歷史存檔與重播、可切換多家 LLM provider、把整體成果自動 commit 回主 repo。
