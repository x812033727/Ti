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
⓪需求澄清   PM 評估需求：模糊就反問關鍵問題（附預設假設）等你回覆，逾時按假設續行；
            結論固化 PRD.md（「願景:」自動回填專案）
①需求拆解   PM 參考 PRD／調研／過往決策，拆成結構化任務 + 驗收標準 + 執行指令
②架構辯論   工程師 ⇄ 高級工程師 來回討論整體做法（有架構師則由其定案；
            開 TI_ADR 時決策沉澱 DECISIONS.md＋adr.json）
③逐任務迭代  for 每個任務（看板 todo→doing→review→done）：
              工程師實作（交付前自測）→ smoke-run + git commit
              → 驗證工程師測試 → 高級工程師審查（帶入測試 log）
              → 通過？否則把【測試+審查意見】原文回饋，重跑（每任務最多 3 輪）
              （可選並行：獨立任務分波，每波多條支線各自 worktree 同時做，再合併回主幹）
④最終 Demo   實際執行整體產出，顯示 stdout/stderr
⑤驗收+檢討   PM 判定完成 → 團隊回顧 → 教訓入庫、後續任務回填 backlog
```

- **需求澄清（預設開啟）**：丟一句模糊需求（「做個記帳的」）時 PM 會先反問最多 4 個關鍵問題
  （各附預設假設），用插話框回覆即可；約 3 分鐘未回覆就按假設續行，不會卡死
  （`TI_CLARIFY` / `TI_CLARIFY_TIMEOUT` / `TI_CLARIFY_MAX_QUESTIONS`）。
- **知識沉澱（預設開啟）**：研究員調研結論持久化到 workspace 的 `docs/RESEARCH.md`
  （PRD 在根目錄 `PRD.md`；設計決策見 `TI_ADR`），下場開場注入——專案模式
  workspace 固定，知識跨場次累積、調研不重查（`TI_KNOWLEDGE`）。
- **人類可中途插話**：執行中於插話框輸入指示，專家會在下一步納入考量；亦可隨時「停止」。
- **任務並行（預設開啟）**：PM 標注依賴、獨立任務分「波次」，每波多條支線各自 git worktree
  分支 + 獨立專家團隊同時做，完工依序合併回主幹（設定面板或 `TI_PARALLEL_TASKS` 切換；預設開啟，設 `0` 還原純循序）。
- **階段性 git**：每輪在 workspace 內的獨立 repo 自動 commit，留下可追蹤歷史。
- **詳細 log**：自測與 Demo 的完整輸出都會回報到討論串（可展開查看）。
- **歷史存檔/重播**：每次 session 的事件自動存檔，可從「📜 歷史」面板挑選並重播當時的討論過程。
- **專案與持續改良**：把產品建成「專案」後，程式碼與改良任務跨場次累積；勾選「♻️ 持續改良」
  即讓團隊自動消化改良任務、自己「找問題」產生新任務，一直改良直到你喊停（見「[專案與持續改良](#專案與持續改良一直找問題一直改良)」）。
- **成果發佈到 GitHub**：設定 token 與目標 repo 後，可手動（或自動）把 workspace 成果推成分支並開 PR。
- **成果匯出下載**：產出檔案面板的「⬇️ 下載成果」按鈕會把該 session 的 workspace 打包成 zip 下載（自動排除 `.git/` 等雜訊）。
- **成果記分卡**：每場 session 收尾自動統計任務完成數、每任務輪數、退回原因（QA/自測/客觀閘門/異議/停滯）；
  「📊 指標」面板跨場顯示成功率、一次過率與「近 10 場 vs 前 10 場」趨勢——讓「越做越進步」看得見。
- **網站/服務的 HTTP 驗收**：PM 或工程師宣告 `Demo 網址: http://localhost:<port>/...` 後，
  自測與最終 Demo 改走「啟動服務 → 輪詢探測 → GET 取狀態碼與內容 → 自動收掉」，
  常駐 server 不再傻等逾時，「驗證: PASS」對 web 產品也可信（僅限 localhost）。

## 角色

| 角色 | 職責 | 工具 |
|------|------|------|
| 🧭 專案經理 | 拆解需求、定驗收標準與執行指令、判斷完成、主持檢討 | 唯讀 |
| 👩‍💻 工程師 | 實際撰寫/修改程式碼、交付前自測、依意見修正 | Read/Write/Edit/Bash |
| 🔬 驗證工程師 | 撰寫並執行測試、回報 pass/fail 與 log | Read/Write/Edit/Bash |
| 🧠 高級工程師 | 參與架構辯論、審查品質/設計/安全、核可或退回 | 唯讀 + Bash |

## 執行環境前置

零先備知識也能照做。本專案統一使用專案內的虛擬環境 `.venv`，**需 Python ≥ 3.10**
（對齊 `pyproject.toml` 的 `requires-python`）。下列範例以 macOS / Linux 為主，
完整路徑寫法 `.venv/bin/python3` 可**免 activate**直接使用（避免誤用系統 Python）；
**Windows 對應為 `.venv\Scripts\python`**（啟動則為 `.venv\Scripts\activate`）。

**前置條件 checklist**（開工前先備齊，依「依賴／secrets／token」三類）：

<!-- 維護注意：勿在此 checklist 寫出 TI_AUTOPILOT_* 完整變數名，首現須留在下方「[設定](#設定)」表。 -->

- **依賴**：Python ≥ 3.10（對齊 `pyproject.toml` 的 `requires-python`）、`git`。
- **secrets**：`ANTHROPIC_API_KEY`（**必備**，預設由 Claude 後端驅動專家；切換 OpenAI 見下方「[設定](#設定)」）。
- **token／選填**：`GITHUB_TOKEN`（發佈成果到 GitHub 時才需）、登入密碼（啟用登入門禁才需，見「[登入 / 門禁（選填）](#登入--門禁選填)」）。

### 1. 建立虛擬環境

```bash
python3 -m venv .venv
```

> 預期結果：專案根目錄出現 `.venv/` 目錄。`.venv/` 已列入 `.gitignore`，**不會進版控**。

### 2. 啟動（互動開發用，跨平台）

```bash
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows（PowerShell / CMD）
```

> 預期結果：終端機提示符前出現 `(.venv)`。
> 本段範例採 `.venv/bin/python3` 完整路徑，故此步可省略；CI 或腳本建議免 activate。

### 3. 安裝套件（含開發工具 extras）

```bash
.venv/bin/python3 -m pip install -e ".[dev]"     # 開發必裝（pytest / ruff / pre-commit）
.venv/bin/python3 -m pip install -e ".[openai]"  # 選用：要切到 OpenAI / 本地模型再裝
```

> 預期結果：`.venv/bin/python3 -m pip list` 列表中可看到 `ti-studio`（editable）。

### 4. 驗證環境

```bash
.venv/bin/python3 -c "import studio; print('ok')"     # macOS / Linux
.venv\Scripts\python -c "import studio; print('ok')"  # Windows
```

> 預期結果：輸出 `ok`，代表 `.venv` 與套件安裝皆正確，可進入下方「安裝 / 啟動」。

### 首次設定 happy-path（從零到啟動，可整段複製）

第一次上手照這條最短路徑跑完即可啟動；複雜旗標不在此展開，詳見下方「[設定](#設定)」表。

```bash
git clone https://github.com/x812033727/Ti.git && cd Ti     # 已 clone 可略
python3 -m venv .venv                                        # 1. 建虛擬環境
.venv/bin/python3 -m pip install -e ".[dev]"                 # 2. 裝套件（含開發工具）
cp .env.example .env                                         # 3. 建 .env，填入 ANTHROPIC_API_KEY
.venv/bin/python3 -m pre_commit install                     # 4.（選填）裝 git hook，提交前自動 lint
.venv/bin/python3 -m studio.server                           # 5. 啟動（Windows：.venv\Scripts\python -m studio.server）
```

> 預期結果：終端機顯示伺服器啟動於 `0.0.0.0:8000`；瀏覽器開 http://localhost:8000 即見工作室首頁。
> 想無金鑰先試流程，把第 5 步換成 `TI_OFFLINE=1 .venv/bin/python3 -m studio.server`（見「離線示範模式」）。

## 安裝

需要 Python 3.10+ 與 [Claude Code](https://code.claude.com) 執行環境。
套件安裝請依上方「[執行環境前置](#執行環境前置)」第 3 步完成（`.venv/bin/python3 -m pip install -e ".[dev]"`），
之後只需準備環境變數檔：

```bash
cp .env.example .env        # 填入 ANTHROPIC_API_KEY
```

## 啟動

```bash
export ANTHROPIC_API_KEY=sk-...                      # 或寫在 .env
.venv/bin/python3 -m studio.server                   # 或：.venv/bin/python3 -m uvicorn studio.server:app
# Windows：.venv\Scripts\python -m studio.server
```

開啟瀏覽器 http://localhost:8000 ，輸入需求（例如「做一個能計算 BMI 並分類的 Python CLI」），
按「開始討論」即可觀看專家協作。產出的程式碼會放在 `workspaces/<session_id>/`。

討論產出檔案後，右側「產出檔案」面板會出現「⬇️ 下載成果」按鈕，點擊即可把整個 workspace
打包成 `workspace-<session_id>.zip` 下載到本機（自動排除 `.git/` 等雜訊；門禁啟用時需先登入）。

### 登入 / 門禁（選填）

本專案有兩層用途不同、各自獨立的「門禁」，分開設定：

#### (A) 登入門禁（最小啟用）

預設不需登入。若要讓工作室只開放給知道密碼的人，設定一組共用密碼即可：

```bash
TI_ACCESS_PASSWORD=你的密碼 .venv/bin/python3 -m studio.server
```

啟用後，未登入者會被導向登入頁，所有 API 與 WebSocket 都需登入才能使用；右上角會出現
「登出」按鈕。登入狀態以簽章 cookie 維持（預設 7 天，見 `TI_AUTH_TTL`）。
未設定 `TI_ACCESS_PASSWORD` 時門禁完全停用，本地開發與離線示範不受影響。

#### (B) Autopilot 門禁前置（自動合併前必做）

<!-- 維護注意：勿在此小節寫出 TI_AUTOPILOT_* 完整變數名，首現須留在下方「[設定](#設定)」表。 -->

啟用 autopilot 自動合併（force-push／merge-admin 等安全旗標）前，務必先在 GitHub 目標分支備妥保護，否則等於把合併閘門大開：

1. 為目標分支設定 **branch protection 或 ruleset**（要求先開 PR、必過 status check 才能合併）。
2. 把 CI 的 `lint`／`test`／`sandbox-test` 三個 job 設為 **required checks**，確保自動合併前一定先綠燈。

各旗標的預設值、風險與解析規則一律只連結不展開，詳見下方「[設定](#設定)」表與其「[Autopilot 安全旗標補充](#autopilot-安全旗標補充)」小節。

### ⚙️ 設定頁（API key / provider / 模型 / GitHub token）

不想改環境變數也可以直接在網頁上設定：按右上角「⚙️ 設定」，即可填入

- 後端 provider（claude / openai）
- Claude API key、Claude 主力 / 快速模型
- OpenAI API key、Base URL（可指向本地模型）、OpenAI 模型
- GitHub token、發佈目標 repo
- 任務並行（開關 / 每波支線數上限）
- **進階流程**開關：需求澄清、卡關討論 huddle、異議檢查 critic、共用筆記、跨場次教訓、反思記憶、客觀驗收閘門、單輪自我精修、子進程資源上限

儲存後會寫入伺服器的 `.env` 檔（已被 git 忽略），並**於下次討論即時生效，無需重啟**。
秘密欄位（key / token）在頁面上不會回顯明文，留空代表「不變更」。
「進階」組對應 `.env` 的 power-user 旗標（見下方[設定](#設定)表），多數情境保留預設即可。

設定面板底部還有「**存取密碼（登入門禁）**」區塊,可直接變更登入密碼:
門禁已啟用時需先輸入目前密碼;門禁未啟用時則可在此設定一組密碼以首次啟用。變更會寫入
`.env`(`TI_ACCESS_PASSWORD`)並即時生效;既有登入不會因此被登出(cookie 以 `TI_AUTH_SECRET`
簽章,與密碼無關)。

#### (C) 反向代理部署（X-Forwarded 信任鏈）

把工作室放在負載平衡器／反向代理（Nginx、Traefik、ELB…）後面時，client 真實 IP 來自
`X-Forwarded-*` 標頭。若不限制誰能設這些標頭，攻擊者可直連 app port 偽造 `X-Forwarded-For`
冒充來源 IP，污染日誌、稽核、限流與 IP 白名單。本專案有**兩層獨立**的防線，各自設定：

| 層級 | 設定 | 由誰處理 | 作用 |
|---|---|---|---|
| 傳輸層 | `TI_FORWARDED_ALLOW_IPS` | uvicorn `ProxyHeadersMiddleware` | 僅受信來源送來的 `X-Forwarded-*` 才被採信、改寫 ASGI scope 的 client IP/scheme |
| 應用層 | `TI_TRUST_PROXY` / `TI_TRUSTED_PROXIES` | `studio/netutil.py` | 由右往左跳過受信代理、取最右非受信位址為真實 client，**不採信最左偽造值** |

部署建議：

- 把 `TI_FORWARDED_ALLOW_IPS` 設為 proxy 的私網範圍（例如 `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`，
  K8s／Swarm 等 proxy IP 會變動的環境需用 CIDR——故依賴下限鎖在 `uvicorn>=0.31`）。
- **嚴禁 `"*"`**（官方明確警告）；本專案偵測到 `"*"` 會 fail-closed 拒啟動。
- proxy 端先 **strip 外部傳入的 `X-Forwarded-*`** 再自行附加（雙重防線）。
- 確保 app port 只有受信代理連得到（防火牆／僅綁私網），避免攻擊者繞過 proxy 直連。

### 在現有的 GitHub 專案上工作

想讓專家討論 / 修改一個現有專案，而不是從零開始：在頂部「GitHub repo 網址」欄位填入
倉庫網址（例如 `https://github.com/owner/repo`），按「開始討論」即可。系統會先把該 repo
**clone 進這次的 workspace**，PM 會先閱讀現有結構再拆解任務。私有倉庫請先在設定頁填入
GitHub token。（僅支援 github.com 的 https 網址；離線示範模式會忽略此欄位。）

### 專案與持續改良（一直找問題、一直改良）

一次性的討論結束就散場；想讓團隊**對同一個產品做下去**，就建一個「專案」：

1. 頂部下拉選單選「➕ 新增專案…」，填名稱與一句話產品願景。
2. 之後選定該專案再「開始討論」，團隊就在專案的**固定 workspace** 上工作——程式碼與
   git 歷史跨場次累積，檢討時發現的後續任務自動排進專案的改良 backlog。
3. 勾選「**♻️ 持續改良**」再開始（需求欄可留空），團隊會進入自動迴圈：
   逐一消化 backlog 裡的改良任務（每個任務跑一場完整討論），backlog 空了就由資深專家
   審視產品現況「**找問題**」、產出新的改良任務，繼續做——一直到你按「停止」、達到輪數
   上限，或再也找不出新改善點為止。執行中照樣可以隨時插話下指示。

每一輪討論都各自存進「📜 歷史」可重播；專案的 backlog 也可透過 `/api/projects` 系列 API
查看與手動排任務。離線示範模式（見下節）同樣支援整套專案／持續改良流程，可先無金鑰試玩。

### 離線示範模式（不需 API 金鑰）

想先試用整套流程、或在沒有金鑰的環境驗證，可開啟離線模式：用腳本化的假專家驅動
**真實的** 流程（真的寫檔、git commit、執行 Demo）。

```bash
TI_OFFLINE=1 .venv/bin/python3 -m studio.server
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
| `TI_CLARIFY` / `TI_CLARIFY_TIMEOUT` / `TI_CLARIFY_MAX_QUESTIONS` | 需求澄清：拆解前 PM 先反問關鍵問題（附預設假設），插話框回答即可；逾時按假設續行、結論固化進 `PRD.md`、抽出的「願景:」回填專案。僅互動討論生效（autopilot／持續改良迴圈自動跳過） | 開啟 / 180 / 4 |
| `TI_KNOWLEDGE` / `TI_KNOWLEDGE_MAX_CHARS` | 知識沉澱：調研結論持久化到 `docs/RESEARCH.md`，下場開場注入尾段（專案模式跨場次累積；設計決策見 `TI_ADR`） | 開啟 / 4000 |
| `TI_DISCOVER_ROLES` | 持續改良「找問題」視角（csv）：senior 工程品質／pm 用戶價值／researcher 上網調研，多視角並行再彙整去重 | senior,pm,researcher |
| `TI_LESSONS` / `TI_LESSONS_MAX` | 跨場次教訓庫（長期記憶）：每場檢討蒸餾可重用教訓存入 `lessons.json`，下次開場注入 PM 拆解，讓工作室越做越會。注入時**按本次需求相關性挑選**（IDF 加權，無人機的坑不會混進網站任務；無相關才退回最新）／`MAX` 為注入筆數 | 開啟 / 12 |
| `TI_LESSONS_DISTILL` / `_THRESHOLD` / `_INTERVAL` | 教訓語意蒸餾：庫內 global 教訓超過 `THRESHOLD` 時，於檢討後用一次 LLM 把相近教訓合併、淘汰過時項（取代純 FIFO 截斷），兩次蒸餾最少間隔 `INTERVAL` 秒。LLM 失敗/離線/壞輸出一律保留原庫（絕不清空長期記憶），行為退回 FIFO | 開啟 / 200 / 86400 |
| `TI_BLUEPRINT` / `TI_BLUEPRINT_SEED_MAX` | 產品藍圖：持續改良迴圈開跑時 PM 把願景展開成結構化藍圖（願景/用戶/功能 P0~P2/里程碑），落盤 `BLUEPRINT.md`＋`blueprint.json`、功能餵入專案 backlog（P0 優先出列，先於手排任務的預設 P1）；之後每輪改良與專案單場討論都注入藍圖前綴。每專案僅生成一次；解析失敗降級存原文、不擋迴圈。進階開關（env 或設定面板「進階」組）／`SEED_MAX` 為一次最多餵 backlog 的功能數 | 關閉 / 5 |
| `TI_ADR` / `TI_ADR_MAX` | 架構決策記錄（ADR）：架構辯論／架構師定案後蒸餾成決策條目，落盤 workspace 的 `DECISIONS.md`（進交付物與 git）＋`adr.json`；後續場次的 PM 拆解與架構提案注入既有決策摘要，翻案須說明理由。進階開關（env 或設定面板「進階」組）／`MAX` 為注入時取最新筆數 | 關閉 / 8 |
| `TI_RESEARCH_TOOLS` / `TI_RESEARCH_ALLOWED_DOMAINS` | 實作中即時研究：開啟後工程師／高級工程師附加 `WebSearch`/`WebFetch`，動工中可上網查官方 API、套件用法與最佳實踐（Claude 路徑 SDK 原生；OpenAI 路徑由 `web_fetch` 工具承接）。研究流量受網域白名單與 SSRF 防護（私網/loopback 位址永遠擋）限制；逾時/無網路自動降級「無調研續行」。註：Claude 的 `WebSearch` 流量在 Anthropic 端、無法施加本地白名單。進階開關（env 或設定面板「進階」組） | 關閉 / 空（不限網域） |
| `TI_REFLEXION` / `TI_REFLEXION_MAX` | 任務級反思記憶（補「只帶上一輪原文」缺口）：失敗輪把 QA/高工意見蒸餾成反思存 per-session JSONL，後續輪/huddle 重試 prepend 回工程師 context／`MAX` 為注入筆數。進階開關（env 或設定面板「進階」組） | 開啟 / 5 |
| `TI_OBJECTIVE_GATE` | 客觀驗收閘門：交付前自測「實際執行」失敗 → 該輪強制退回，不讓 QA/高工的文字裁決推翻真實 exit code（守住反 reward-hacking）。`1`=工程師本輪宣告的自測指令實敗才否決（fallback 整體指令只回報不硬退）；`strict`=fallback 失敗與「未宣告執行指令」皆視為未通過 | 1（開啟） |
| `TI_SELF_REFINE_ITERS` | 單輪內自我精修：自測未過時讓同一工程師就地依執行紀錄再修一次（交付驗證前），上限 N 次 | 1（開啟） |
| `TI_RLIMITS` / `TI_RLIMIT_MEM_MB` / `TI_RLIMIT_CPU_S` / `TI_RLIMIT_FSIZE_MB` | 子進程資源上限：runner 執行指令時套 RLIMIT，補 bwrap 沒有的記憶體/CPU/檔案大小防線（各上限 0=略過該項） | 1 / 4096 / 300 / 512 |
| `TI_DEMO_TIMEOUT` / `TI_DEMO_MAX_OUTPUT` | 自測/Demo 的逾時秒數與輸出字數上限 | 60 / 8000 |
| `TI_ENABLE_GIT` | 是否在 workspace 內做階段性 commit | 1 |
| `TI_HOST` / `TI_PORT` | 伺服器位址 | 0.0.0.0 / 8000 |
| `TI_ACCESS_PASSWORD` | 設定後啟用登入門禁（共用密碼） | 未設定（停用） |
| `TI_AUTH_SECRET` / `TI_AUTH_TTL` | cookie 簽章密鑰 / 登入有效秒數 | 隨機 / 604800 |
| `TI_FORWARDED_ALLOW_IPS` | uvicorn ProxyHeaders 信任來源（傳輸層）：僅清單內來源送來的 `X-Forwarded-*` 會被採信改寫 client IP/scheme。預設僅本機；嚴禁 `"*"`（偵測到即拒啟動）。反向代理部署見下方小節。別名 `FORWARDED_ALLOW_IPS` | 127.0.0.1 |
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
| `TI_AUTOPILOT_EVAL_MEMORY` | 自我評估（backlog 空時找改善點）回饋給專家的「近期成敗」筆數（done/failed 各取最新 N 筆，附失敗原因）。讓迴圈記取自身成績單——避免重提已完成、避開已知失敗做法；越跑越聚焦。0=停用（無狀態評估） | 20 |
| `TI_PROJECTS_ROOT` | 專案（長期產品）meta 與專屬 backlog 的存放根目錄 | `projects/` |
| `TI_IMPROVE_MAX_CYCLES` | 持續改良迴圈單次連線最多跑幾輪（每輪＝一場完整討論）；0=不限（直到找不到新改善點） | 5 |
| `TI_IMPROVE_MAX_FAILS` / `TI_IMPROVE_COOLDOWN` | 連續失敗幾輪即停 ／ 每輪之間喘息秒數 | 2 / 0 |
| `TI_HISTORY_MAX_COUNT` / `TI_HISTORY_MAX_AGE` | 自動回收：最多保留幾個非 running session ／ 最後活動超過幾秒即回收（含 history 的 meta+events 與其 workspace 產出）；0=該規則停用 | 200 / 0 |
| `TI_MAX_CONCURRENT_SESSIONS` | 同時進行的討論場次上限（每場會起多個專家子程序/LLM 連線）；超過時新的 `/ws` 連線被拒（送 error 後 close 1013）。0=不限 | 8 |
| `TI_REQUIRE_CHOWN` | root-only state 寫入（history meta/events、backlog.json）的擁有者強制驗證模式：`strict`=驗證未過即 fail-closed（拒寫、不留半成品）／`warn`=記錄後放行／`off`=顯式逃生開關，靜默放行。不認得的值 fail-safe 取 `strict`。**預設 `strict`（安全側，breaking change）**，詳見下方「root-only 寫入保護」補充 | strict（預設安全） |

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

#### root-only 寫入保護（`TI_REQUIRE_CHOWN`）

所有「只應由 root 持有」的 state 檔（history 的 meta/events 與 backlog.json）都經由單一收斂點
`studio.secure_write.secure_write_root` 寫入：原子寫入＋反 symlink TOCTOU＋chown 後以 fd 複驗
擁有者（uid==0、非 hardlink），不信任 chown 回傳值。

- **Breaking change**：本旗標**預設為 `strict`**（fail-closed）。在**非 root 部署**下，原本能成功的
  state 寫入會因 chown 失敗而被拒（raise），這是相對舊版的破壞性變更。
- **遷移指引**：非 root 部署若尚無法滿足 root 擁有者要求，過渡期請顯式設 `TI_REQUIRE_CHOWN=warn`
  （照寫但記 WARNING），確認影響後再評估。完全停用驗證請設逃生開關 `TI_REQUIRE_CHOWN=off`
  （`off` 為靜默放行，安全保證被放寬，僅限明確知道風險時使用）。
- 顯式設為任何非 `strict` 值時，config 載入會記一條明顯 WARNING 提醒安全保證被降級。

### 切換到 OpenAI / 本地模型

```bash
.venv/bin/python3 -m pip install -e ".[openai]"
TI_PROVIDER=openai OPENAI_API_KEY=sk-xxx .venv/bin/python3 -m studio.server
# 本地模型（OpenAI 相容，如 Ollama）：
TI_PROVIDER=openai OPENAI_BASE_URL=http://localhost:11434/v1 TI_OPENAI_MODEL_LEAD=llama3.1 .venv/bin/python3 -m studio.server
```

## 測試

不需 API 金鑰即可跑流程狀態機單元測試——以 pytest 執行測試、ruff 做 lint / 格式檢查、pre-commit 裝提交前 hook。
安裝、測試、lint 與 pre-commit 的**完整可複製指令**統一收錄於 [CONTRIBUTING.md](CONTRIBUTING.md)（Linux/macOS 為準，Windows 改用 `.venv\Scripts\python`，詳見該文件）。

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
  projects.py      專案（長期產品）：固定 workspace、專屬 backlog、session 足跡
  improver.py      專案持續改良迴圈：消化 backlog → 跑討論 → 回填 → 找問題
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
