## 任務 #1 完成：新增 `studio/discussion.py` 通用 DiscussionEngine：支援任意 N 個角色（ExpertLike 介面）、`round_robin`（依序）與 `parallel`（同輪並行、輪間同步，asyncio.gather＋複用 `_llm_semaphore()` 節流）兩種模式；context 餵法為「議題＋上一輪全員發言＋自己的歷史發言」而非全史重播；回傳結構化 transcript（輪次/角色/全文）

## 任務 #2 完成：實作互相回應與反諂媚機制：發言 prompt 要求 `回應 @角色名: 同意|反對 ＋理由` 結構化引用、且「至少指出一個可挑戰點，無異議須說明為何」；在 discussion.py 加 `parse_mentions()` 解析發言中的 @引用為結構化記錄（防禦式：格式不符整段視為無引用，不 silent 錯位）

## 任務 #3 完成：實作收斂控制與討論小結：最大輪數上限（`TI_DISCUSS_MAX_ROUNDS`）＋沿用 `flow.is_stalled()` 相似度自適應提前停止；討論結束輸出小結結構（共識清單/分歧清單/各角色最終立場），供後續結論彙整使用

## 任務 #4 完成：接線與設定：orchestrator 的 `_debate()` 在 `TI_DISCUSS_MODE=round_robin|parallel` 時改走 DiscussionEngine（未設或 `legacy` 時行為與現狀完全一致）；`config.py`/`settings.py` 白名單加新欄位、`.env.example` 補說明；更新 ARCHITECTURE.md 並在 discussion.py 模組 docstring 寫明**實際函式簽名**

## 任務 #5 完成：撰寫離線測試 `tests/core/test_discussion.py`：用 StubExpert 驗證依序/並行模式的發言順序與輪間同步、@引用解析（含格式不符退化案例）、最大輪數與 is_stalled 提前停止、`TI_DISCUSS_MODE` 未設時 `_debate` 走舊路徑

## 任務 #1 完成：建立角色設定檔載入器：新增 `studio/role_store.py`，定義角色檔格式為 `roles/*.md`（YAML frontmatter 放 key/name/avatar/title/model/allowed_tools/permission_mode/tags/description，body 即 system_prompt），用 pydantic 驗證後轉 frozen Role；啟動時以「內建 8 角色為預設、檔案同 key 覆蓋」合併進 `ROSTER`/`BY_KEY`，壞檔案明確拒絕（記 log、不影響內建角色），並附離線單元測試（檔案覆蓋內建／壞檔被拒／未知 frontmatter 欄位明確報錯）

## 任務 #2 完成：實作角色管理 API：在 routes.py 加 `GET/POST/PUT/DELETE /api/roles`（照既有 pydantic Body model＋auth 依賴慣例），寫入即落檔到 `roles/` 並 reload 角色表；內建角色可被覆蓋、不可刪除（刪除覆蓋檔＝還原內建）；建立/更新時驗證 system_prompt 非空且含出力格式段落（micro-rules，拒絕空殼 persona）；附 API 層測試

## 任務 #3 完成：實作討論小組（Group）：新增 `Group = {name, role_keys[], mode}` 概念與 `GET/POST/PUT/DELETE /api/groups`，存檔於 `roles/groups.yaml`（或同目錄設定檔）；組隊三條硬規則——role_key 必須存在、不得重複、≥2 人，違反即 4xx 明確報錯；mode 白名單沿用 `{round_robin, parallel}`，非法值報錯；附測試（含「引用不存在角色被拒」）

## 任務 #4 完成：同步文件與設定：把 #1~#3 的角色檔格式、API 欄位（request/response 每個欄位名與型別）寫入 ARCHITECTURE.md（或 docs/）；roles/ 目錄加一份範例角色檔；`.env.example` 與 settings.py 若有新增環境變數一併補上

## 任務 #5 完成：冒煙驗證：實際啟動 `python3 -m studio.server`，用 HTTP 走完「建角色→列出→編輯→組隊→刪除還原內建」全流程並核對回應；同時跑全測試套件確認既有測試零回歸（特別是依賴 `BY_KEY`/`ROSTER` 的 discussion/orchestrator 路徑）

## 任務 #1 完成：補齊內建 8 角色的 `description` 一句話職能描述（roles.py 一處），並加守門單測驗證 8 角色 description 全數非空

## 任務 #2 完成：在 flow.py 新增 `子題:` 與 `負責:` 行前綴 parser 純函式（議程含標題＋描述＋成功準則；含條列 fallback、允許探索型單子題不硬拆），附單元測試

## 任務 #3 完成：改造拆解 prompt 與 orchestrator 呼叫端：拆解階段一次輸出「議程（2–5 子題）＋既有 `任務:`/`#id`/`依賴:` 格式＋每子題 `負責: <role_key>`」，程式碼硬驗證 role_key 存在於可用角色集、非法或缺漏 fallback 至 engineer，討論階段逐子題以 topic 餵 DiscussionEngine（不改引擎內部）

## 任務 #4 完成：將拆解結果（議程、任務、分派表）持久化進既有 history/session 機制，可供事後重看

## 卡關討論 任務 #5：冒煙驗證：以 fake experts 真實啟動 server，走「需求→議程拆解→分派→逐子題討論→彙整」全流程並確認無回歸
【專案經理】先講診斷結果（我實跑驗證過，不是猜的）：

## 卡關根因：不是程式壞，是「執行指令」在驗證環境跑不起來

1. **驗證環境只有 `python3`，沒有 `python`**（`command -v python` 查無）。我們宣告的 `執行指令: python -m studio.server` 一執行就 command not found → Demo 網址輪詢必失敗 → 連續紅。
2. 反證：我改用 `python3 -m studio.server` 真實啟動，`curl http://localhost:8765/login` 回 **200**；冒煙測試 `python3 -m pytest tests/server/test_smoke_agenda_real_server.py` **1 passed**。程式本體與冒煙流程是好的。

## 可立即執行的突破做法（按序）

- **#5a 改執行入口宣告**（一行改完）：執行指令與驗收標準裡所有 `python` 改 `python3`。即：`執行指令: python3 -m studio.server`、回歸跑 `python3 -m pytest tests/ -q`。
- **#5b 加可攜入口防呆**（工程師 5 分鐘）：`scripts/serve.sh` 用 `PY=$(command -v python3 || command -v python)` 包一層；之後任何環境都不再踩同一坑。`smoke_agenda_run.sh` 內部已用 `sys.executable`/腳本路徑的照舊，不必動。
- **#5c 驗證工程師複跑指令固定為**：
  1. `python3 -m pytest tests/server/test_smoke_agenda_real_server.py -q`（真實 server 全流程，含自證回指）
  2. `python3 -m pytest tests/ -q`（零回歸，全套件我已在背景跑，結果出來即確認）

修正後的整體宣告：

執行指令: python3 -m studio.server
Demo 網址: http://localhost:8000/login

一句檢討先記著：驗收標準寫的指令，拆解當下就該在目標環境實跑一次確認可執行——這次兩輪紅全是入口指令可攜性，最便宜的階段沒攔住。等背景全套件結果出來我就下決議。
【架構師】PM 的診斷我認可——根因是入口指令可攜性，不是設計或程式回歸。我快速確認一下現況再定案。
確認屬實：環境只有 `/usr/bin/python3`，無 `python`；`scripts/serve.sh` 尚不存在。PM 方案採納，補一個防回歸的細節後定案：

**架構視角的補充**：這次失敗在「宣告」層而非「程式」層——驗收標準裡寫死直譯器名稱本身就是不可攜假設（與我們 shell 偵測腳本的可攜性教訓同型）。所以不只改一行，要把入口收斂到單一可攜包裝，宣告引用包裝而非裸指令，避免下次任何人再手寫 `python` 復發。

設計決策: 執行入口宣告改為 `python3 -m studio.server`，Demo 網址不變（http://localhost:8000/login）；驗收標準內所有 `python -m pytest` 同步改 `python3 -m pytest`
設計決策: 新增 `scripts/serve.sh` 作為唯一啟動入口——`PY=$(command -v python3 || command -v python)` 後 `exec "$PY" -m studio.server "$@"`；純 POSIX sh、零新依賴，沿用既有 scripts/ 慣例
設計決策: 介面收斂——文件與後續任務宣告一律引用 `bash scripts/serve.sh`（或明寫 python3），禁止再出現裸 `python` 前綴；測試腳本內部繼續用 `sys.executable`，不受影響
設計決策: 模組邊界不動——flow.py / orchestrator / discussion.py / events.py 本輪零改動，此 blocker 不觸發任何設計翻案
設計決策: 守門補強——冒煙驗證程序固定兩步：① `python3 -m pytest tests/server/test_smoke_agenda_real_server.py -q`（含自證回指）② `python3 -m pytest tests/ -q`（零回歸）；未來新任務的「執行指令」宣告須在拆解當下於目標環境實跑一次才可寫入（攔在最便宜階段）

工程師可立即動工：#5a 改宣告＋#5c 複跑（5 分鐘內），#5b 的 serve.sh 順手補上。無其他阻塞。
【工程師】收到，照 PM/架構師定案直接動工：#5b 寫 `scripts/serve.sh`、#5c 複跑兩步驗證、#5a 修正宣告。先看 scripts/ 既有慣例：
自測 serve.sh：單一命令內啟動→健康檢查→收掉（沙箱跨命令 loopback 不通，故同命令完成）：
卡關突破完成（06328af），三項落地＋一個額外根因修掉：

**#5a/#5b（PM＋架構師定案）**
- 新增 `scripts/serve.sh`：唯一啟動入口，`command -v python3 || command -v python` 偵測直譯器後 `exec`，已自測（背景啟動→`curl /login` 200→收掉）。
- 宣告全面改 `python3`／`bash scripts/serve.sh`，不再出現裸 `python`。

**#5c 固定兩步驗證（已實跑）**
1. `python3 -m pytest tests/server/test_smoke_agenda_real_server.py -q` → **1 passed**（真 uvicorn＋TCP＋WS 全流程，含自證回指）。
2. `python3 -m pytest tests/ -q` → **1912 passed、0 failed、0 errors 全綠**。

**額外戰果：把「移交待辦」的 18 個紅項根治了**。逐層證偽後發現不是沙箱、不是 proxy（no_proxy 已含 127.0.0.1）——是 `config.py` 的 `load_dotenv()` 不帶路徑會從 cwd 向上搜，worktree 跑測試時載到部署機的 `/opt/ti/.env`（含門禁密碼）→ live-server 測試全 401。修成固定載 `PROJECT_ROOT/.env`（與 `env_path()` 寫入端同一路徑，部署布局零行為差），settings 3 failed＋15 errors 全部轉綠。驗收標準 1「全套全綠」首次真正達成。

檢討一句：前兩輪我把這組紅項標成「既有環境問題、移交」就停手——stash 對照只證明「非我引入」，沒繼續問「根因是什麼、修得掉嗎」。診斷到可修的根因才算完，這條記入經驗。

執行指令: bash scripts/serve.sh
Demo 網址: http://localhost:8000/login
【高級工程師】先看突破 commit 的完整 diff：
## 卡關突破審查結論（commit 06328af）

**全部親自實跑驗證**：
1. 固定兩步驗證：真實 server 冒煙＋議程 e2e → **3 passed**；全套回歸 → **1912 passed, 0 failed, 0 errors**（首次真全綠）。
2. `serve.sh` 實跑：啟動 → `/login` 200 → 乾淨收掉。語法檢查過、ruff 全綠。

**逐項審查**：
- **serve.sh**：`PY="$(command -v python3 || command -v python)" || {...}` 寫法正確（替換失敗才走 127 分支）、`cd` 到 repo root、`exec` 透傳參數——可攜性教訓正確落地。
- **config.py dotenv 修復是本次最有價值的一刀**：根因診斷正確（與我第二輪獨立查到的根因一致——`load_dotenv()` 向上搜到 `/opt/ti/.env` 部署密碼）。修法對：載入路徑釘到 `PROJECT_ROOT/.env`，與寫入端 `env_path()` 同一單一來源。我已核實部署根 `/opt/ti/studio/config.py` 存在 → 部署機 `parent.parent` 仍是 `/opt/ti`，**零行為差宣稱屬實**。且這同時是安全改進：測試/開發進程不再悄悄吸入部署機的門禁密碼與金鑰。
- 工程師的檢討（「移交≠診斷完成，要追到可修根因」）值得入庫，這次他比我上一輪的「移交待辦」多走了正確的一步。

**非阻塞小項**（跟進即可，不退回）：
- `config.py` 第 16 行的 `Path(__file__).resolve().parent.parent` 與檔尾 `PROJECT_ROOT` 重複——下次經過時把 `PROJECT_ROOT` 定義上移、load_dotenv 引用它，單一來源更乾淨。

驗收標準 1（全套全綠）、7（真實啟動 server＋自證回指）至此全數達成，入口指令可攜性根治。

`決議: 核可`

## 任務 #5 已知限制：冒煙驗證：以 fake experts 真實啟動 server，走「需求→議程拆解→分派→逐子題討論→彙整」全流程並確認無回歸（huddle 與重試後仍未通過）

