# Ti Studio — AI 協作指南（CLAUDE.md）

本檔是給接手本 repo 的 AI 助手的全景導覽：**這個專案是什麼、程式碼結構、怎麼跑/測/lint、
有哪些不可違反的慣例**。前半段是上手導覽，後半段「專案協作記憶」是歷次任務累積的硬規則與
踩坑教訓，兩者都要遵守。指令的唯一權威是 `CONTRIBUTING.md`、架構細節的唯一權威是
`ARCHITECTURE.md`，本檔僅敘述與交叉連結，不重複可複製的指令區塊（防文件漂移，由 `tests/docs/` 把關）。

> ⚠️ 寫本檔（或任何 repo 文件）時，**禁止裸用 `python` 直譯器**（即後面直接接空白與檔名的寫法）——
> 一律寫 `python3` 或 `.venv/bin/python`。`tests/docs/test_qa_task2_no_bare_python.py` 會全 repo 掃描，CLAUDE.md 不在豁免名單。

## 專案概覽

Ti Studio 是一個 **FastAPI 後端 + 免建置前端（`web/`，純 HTML/CSS/JS）** 的**多智能體軟體開發工作室**。
給它一段產品需求，工作室裡的 AI 專家（專案經理／工程師／高級工程師／驗證工程師…）會自己
討論 → 寫程式 → 測試 → 審查 → Demo → 反覆改進，全程在網頁即時呈現。專家會**真的**寫檔、
跑指令、git commit、執行 Demo 並以 exit code 驗收。

- 語言/版本：Python 3.11+；套件名 `ti-studio`（版本 SSOT 由 `studio.release_note.pyproject_version()` 提供）。
- 主要 LLM 後端：Claude Agent SDK（預設）；可選 OpenAI／MiniMax／Gemini／Codex／Antigravity。
- 文件與程式碼註解皆為**繁體中文**；沿用此慣例。

## 快速上手

完整安裝／測試／lint／pre-commit 指令**以 `CONTRIBUTING.md` 為唯一權威**，請以該檔為準。
最常用的三條速查（細節見 `CONTRIBUTING.md`）：

- 離線示範（**免 API 金鑰**，最快看到完整流程）：`TI_OFFLINE=1 .venv/bin/python -m studio.server`
- 跑全部測試：`.venv/bin/python -m pytest -q`
- Lint：`.venv/bin/python -m ruff check .`

跑測試與離線示範**不需** API 金鑰；只有真正驅動 LLM 專家時才需要 `ANTHROPIC_API_KEY`（或 OpenAI 等設定）。
ASGI 入口為 `studio.server:app`，也可 `python3 -m studio.server` 直接啟動。

## 程式碼結構（模組地圖）

主套件在 `studio/`。權威的模組分工與資料流見 `ARCHITECTURE.md`；下表為導覽濃縮版。

| 分類 | 模組 | 職責 |
|------|------|------|
| 入口/組裝 | `server.py` | 應用組裝：建立 FastAPI app、掛 `/static`、`include_router`、頁面入口、`main()` 啟 uvicorn |
| | `routes.py` | REST API：health、登入/登出、workspace（列檔/讀檔/下載 zip）、history、publish、`/api/roles`、`/api/groups`、`/api/workflows`、`/api/provider-quota`、`/api/settings`、`/api/metrics` |
| | `ws.py` | WebSocket：建 session、串流事件、`_pump_interventions` 收人類插話/停止 |
| | `auth.py` | 單一密碼門禁（HMAC token + httponly cookie） |
| 核心狀態機 | `orchestrator.py` | `StudioSession`：**workflow 直譯器**（`_run_workflow`→`_stage_*`）；預設骨架＝澄清 → 拆解 → 架構討論 → 逐任務分波迭代 → Demo → 驗收/檢討（`workflow=None` 等價骨架）；含 dynamic step（額度感知分派／PM 動態招募） |
| 動態流程 | `workflow.py` | 宣告式流程定義：`Stage`/`Workflow` schema＋validate、`default_workflow()`／`dynamic_first_workflow()`、`workflows.yaml` CRUD；`VERDICTS` 白名單只映射 `flow.py` 判定（見 `docs/workflows.md`） |
| | `provider_quota.py` | provider 即時額度快照＋給 PM 的額度摘要/受限判定/最寬鬆就緒（混合模式額度感知分派與招募重綁的資料源） |
| 純函式決策層 | `flow.py` | **無狀態**解析：`parse_tasks`/`parse_clarify`/`parse_next_step`/`parse_followups`/`build_waves`/`qa_passed`/`senior_approved`/`is_stalled`… 可單元測試、可 monkeypatch |
| 執行與隔離 | `runner.py` | 確定性執行：跑程式/Demo、git（含 worktree 合併）、入口偵測、HTTP 服務驗收、bubblewrap sandbox |
| | `workspace.py` | 每個 session 的沙箱工作目錄（安全路徑、列檔、讀檔、打包 zip） |
| LLM 中介 | `experts.py` | Claude 專家：包裝 `ClaudeSDKClient`，串流回應轉事件；退避工廠 `make_retry_config()` |
| | `providers.py` | provider 抽象與工廠（Claude / OpenAI 相容 / 其他） |
| | `llm_caller.py` | provider 無關的 retry 骨幹（`RetryConfig` + `run_with_retries` + `backoff_delay`，退避公式 SSOT） |
| | `tools.py` | 非 Claude provider 的 function-calling 工具層（read/write/edit/bash…） |
| 討論/角色 | `discussion.py` | 多角色討論引擎（`round_robin` / `parallel`）、反諂媚引用、提前收斂 |
| | `roles.py` | 內建 8 角色（CORE 4 + OPTIONAL 4）+ 共通守則 `_COMMON`；對外 `ROSTER`/`BY_KEY` |
| | `role_store.py` | 自訂角色檔（`roles/*.md`）載入/驗證/原子落檔 + 討論小組（`roles/groups.yaml`）CRUD |
| 持續改良/發佈 | `backlog.py` | 任務佇列 CRUD（read-modify-write + 檔鎖）；以 `state_dir` 泛化「專案 backlog」與「核心 backlog」 |
| | `projects.py` | 專案（長期產品）：固定 workspace、專屬 backlog、跨場次累積；`effective_repo()` 決定發佈目標 |
| | `improver.py` | 專案持續改良迴圈：消化 backlog → 跑討論 → followups 回填 → 空了就「找問題」 |
| | `autopilot.py` | Ti 核心自我改善迴圈（獨立服務）：抽核心 backlog → headless 討論 → 測試 gate → 合併/部署 |
| | `publisher.py` | 把 workspace 成果推成 GitHub 分支並開 PR、等 CI、（選配）合併 |
| | `conclusion.py` | agenda/任務結構、followup 解析、核心改動路由 |
| 設定/狀態 | `config.py` | 集中設定（~140+ 個 `TI_*` 環境變數）+ `reload()` 執行期套用 |
| | `settings.py` | UI 可調設定（API key/provider/模型/GitHub token）：寫 `.env` → `config.reload()` |
| | `history.py` | session 事件存檔（JSONL + meta）、重播、成果記分卡、`/api/metrics` 跨場聚合 |
| | `events.py` | `StudioEvent` 結構（前後端契約） |
| | `fake_experts.py` | 離線示範用假專家（真寫檔/commit，供無金鑰試用與 E2E） |

設計重點：**`flow.py`（純函式）vs `orchestrator.py`（有狀態）** 的邊界要守住——決策解析放 `flow.py`、
所有副作用（broadcast、專家互動、git、workspace 變更）放 `orchestrator.py`。orchestrator 對 flow
函式採 re-export（`from .flow import parse_tasks as parse_tasks …`），讓測試/autopilot 能
monkeypatch `orchestrator.<fn>` 仍生效——新增解析函式時沿用此模式。

## 執行期資料流（精簡）

1. 前端開 WebSocket `/ws`，第一則訊息送 `{requirement, repo_url?, project_id?, mode?, group?}`。
2. `ws.py` 建 workspace、開始錄歷史，啟動 `StudioSession.run()`。
3. orchestrator 依階段推進，透過 `broadcast()` 送 `StudioEvent`（`session_started`/`phase_change`/
   `expert_message`/`tool_use`/`board_update`/`run_result`/`git_commit`/`demo_result`/`done`…）。
4. 每個事件即時渲染並寫入 `history/<id>.jsonl`；前端可送 `{"type":"interject"}` 或 `{"type":"stop"}`。
5. `done` 結束；歷史可從 `/api/history` 列出並重播。

事件型別是前後端契約，定義集中在 `events.py`，前端在 `web/js/events-render.js` 的 `handleEvent()` 對應（`web/app.js` 為 ES module 入口，模組拆分見 `ARCHITECTURE.md`「前端（web/）」）。

## 關鍵慣例（給 AI 的硬規則）

- **雙軌路由（最重要）**：專案改動 → 專案 repo（`projects.effective_repo()`）；**Ti 核心框架改動**
  （orchestrator/runner/發佈流程等）→ `config.CORE_REPO`（固定 `x812033727/Ti`）的**獨立 PR**，
  **絕不混入專案 repo**。偵測靠專家輸出結構化行 `核心改動: <描述>`（`flow.parse_core_changes`），
  消費端 `backlog.add_items(core, source="core")`（省 `state_dir`＝核心 backlog）路由，autopilot 在核心 repo 實作開 PR。
  詳見下方「架構鐵則」與 `ARCHITECTURE.md`。
- **設定走 `config.py` 為 SSOT**：所有行為由 `TI_*` 環境變數定義；UI 改設定 → 寫 `.env` →
  `config.reload()`，下一個 session 生效。不要在各檔散落硬寫預設。
- **解析靠穩定 marker 字串**：如 `驗證: PASS/FAIL`、`決議: 核可/退回`、`任務: #<n>`、
  `依賴: #後 -> #前`、`後續任務: [P0/bug] <title>`、`核心改動: <描述>`——**改動這些字串會破壞解析**，
  動到前先確認 `flow.py` 對應 parser。
- **程式風格**：繁中註解、簡潔 docstring、`from __future__ import annotations`；不隨意新增依賴
  （認證等優先用標準庫）；ruff 規則集中於專案設定檔的 `[tool.ruff]`，勿在個別檔覆寫。
- **測試慣例**：放 `tests/`、檔名 `test_*.py`、`asyncio_mode = "auto"`；端到端走離線假專家
  （`tests/test_offline_e2e.py` / `fake_experts.py`），不依賴外部 API；新增後端能力盡量補對應測試。

## 開發 / CI / 發佈流程（敘述 + 連結）

- **CI**（`.github/workflows/ci.yml`）：`lint`（`ruff check` + `ruff format --check` + shell 掃描 warn +
  bare-pytest 掃描 block）、`test`（Python 3.11/3.12 矩陣，先 `--collect-only` 抓 import 錯，再 `pytest --cov`，
  `TI_SANDBOX=0`）、`deploy-test`（只跑 `tests/deploy`）、`sandbox-test`（bubblewrap + AppArmor + bwrap smoke valve）。
- **安全掃描 SSOT**：`scripts/scan_shell_usage.sh`（偵測 `shell=True` / `create_subprocess_shell`，目前 warn-only）、
  `scripts/scan_bare_pytest.sh`（掃 `docs/`，block）。CI、pre-commit、本機三處只呼叫同一支腳本，規則天然一致。
- **發佈鏈**：`push tags v*` → `publish-release.yml`（PAT guard、assert tag == `pyproject_version()`、
  `scripts/publish_release.py` 渲染 `body.md`、`gh release create` 用 `secrets.GH_PAT`）→ `release: published`
  → `release-smoke.yml`。版本 SSOT = `studio.release_note.pyproject_version()`。**權威細節見下方「發佈鏈 DoD 與 `GH_PAT` 設定」**。

## 延伸閱讀

- `ARCHITECTURE.md` — 模組分工、執行期資料流、認證流程、並行 lane、動態流程、專案與改良迴圈（架構唯一權威）。
- `docs/workflows.md` — 動態流程（Dynamic Workflow）schema、dynamic step、額度感知分派、PM 招募、互動預設。
- `CONTRIBUTING.md` — dev 指令唯一權威（安裝/測試/lint/pre-commit）、風格與提交慣例。
- `DECISIONS.md` — 架構決策記錄（ADR）。
- `README.md` — 產品全貌與功能說明。

---

# 專案協作記憶

> 以下為歷次任務累積的硬規則與工程教訓，是上方導覽的權威細節來源，務必遵守。

## 架構鐵則：專案 repo vs Ti 主核心 repo（雙軌路由）

- **專案改動**進專案 repo（`projects.effective_repo`：per-project `publish_repo` → 全域 `TI_PUBLISH_REPO`）。
- **Ti 核心框架改動**（orchestrator／runner／發佈流程等）一律路由到 **`config.CORE_REPO`（固定
  `x812033727/Ti`）的獨立 PR**，**絕不混入專案 repo**。
- 判定方式：由專家在討論／檢討中以結構化行 `核心改動: <描述>` 表態（`flow.parse_core_changes`），
  消費端以 `backlog.add_items(core, source="core")`（省略 `state_dir`＝核心 backlog）路由，
  autopilot 在核心 repo 實作並開 PR。詳見 `ARCHITECTURE.md`「專案 repo 與 Ti 主核心 repo」。

## 安全自改合約：`_commit_push_merge` 不變式

- `studio/autopilot.py::_commit_push_merge` 入口先檢查 `config.AUTOPILOT_REPO` 不可為空，且
  `config.PUBLISH_REPO` 非空時必須與 `AUTOPILOT_REPO` 指向同一 repo；違反即回 `(False, reason)`，
  不執行 `git push`、開 PR 或 merge flow。
- repo identity 由 `studio/autopilot.py::_repo_key` 正規化為 `github.com/owner/repo`；bare
  `owner/repo`、GitHub HTTPS、GitHub SSH 可視為同一 repo，但同 path 的非 GitHub host 一律視為不符。
- 實際 push 前會讀 `git remote get-url --push origin`，正規化後必須等於 `AUTOPILOT_REPO`；
  不符即中止，避免傳入 clone 的 origin 被改到專案 repo 或偽造同 path host。
- guard 通過後立即 `publisher.set_repo_override(config.AUTOPILOT_REPO)`，並用 `try/finally` 包住後續
  checkout/commit/push/PR/merge 全段，確保任何 publisher REST helper 在 autopilot 路徑都只看見
  `AUTOPILOT_REPO`，且異常時會還原 per-session override。
- 守門測試在 `tests/autopilot/test_qa_no_publish_pollution.py`；黑白樣本需涵蓋
  `PUBLISH_REPO` 空值、同 repo 放行、不同 repo/非 GitHub 同 path 擋下，以及 origin push URL 不符時
  push 前中止。
- 本輪範圍外移交待辦：結構化 `autopilot/audit.jsonl` 審計紀錄、`AUTOPILOT_DAILY_PR_BUDGET`
  每日 PR 成本熔斷。不要混進 repo 污染防護修補。

## 發佈鏈 DoD 與 `GH_PAT` 設定

- 發佈鏈契約：`.github/workflows/publish-release.yml` 只在 `push.tags: v*` 建立 GitHub release；
  `.github/workflows/release-smoke.yml` 只用 `release: published` 接下游 smoke。建立 release 的
  `GH_TOKEN` 必須維持 `secrets.GH_PAT`，不可換回 `GITHUB_TOKEN`，否則 GitHub 防遞迴機制會讓
  `release-smoke` 不被觸發。
- `GH_PAT` 設定指引：建立 Fine-grained PAT；Repository access 務必只選本 repo（非 all-repos）；
  Repository permissions 僅開 `Contents: Read and write`；
  到 repo `Settings -> Secrets and variables -> Actions` 建立 secret，名稱固定為 `GH_PAT`。
- `GH_PAT` 到期或被撤銷時，Step 5 `gh release create` 會以 403 失敗；輪替後只更新同一個 repo
  secret `GH_PAT`，不要改 workflow token 路由。
- 發佈 DoD：`body.md` 必須由 `scripts/publish_release.py` 產生，版本來自
  `studio.release_note.pyproject_version()`，Breaking heading 來自同一 Python SSOT，不在 YAML 硬寫；
  發佈前需重跑 release 相關守護測試與 `python3 scripts/publish_release.py`。
- 驗證邊界必須明講：單元/守護測試為半閉環，真實 `v*` tag-push 端到端尚待生產驗證。換句話說：
  真實 tag-push 端到端尚待生產驗證；第一次正式打 `v*` tag 後，需確認
  `publish-release -> release-smoke` 生產鏈實際通過。
- 移交待辦：真實 `v*` tag-push 生產 E2E 仍是半閉環，正式發 release 時要先人工確認 `body.md`：
  1. 先跑 `python3 scripts/publish_release.py` 產出 `body.md`。
  2. 開 `body.md`，確認頂部就是 `## ⚠️ Breaking Changes`，沒有被放到其他章節後面。
  3. 確認該區塊內仍有四要素與 `TI_REQUIRE_CHOWN=warn/off` 逃生艙。
  4. 再做 `gh release create "$TAG" -F body.md`，並在 GitHub release 頁核對 body 與本機一致。
- 本輪不加 `--verify-tag`：現有觸發條件已由 `push.tags: v*` 保證 tag 存在，且 workflow 另有
  `github.ref_name == v{pyproject_version()}` fail-fast。若未來新增 `workflow_dispatch` 手動發佈，需重審此決策。
- 任務 #3 最小硬化決策：已將 `publish-release.yml` 的 workflow-level `permissions.contents` 下修為
  `read`，因為 `gh release create` 的寫入權限走 `secrets.GH_PAT`；`actions/checkout` /
  `actions/setup-python` 暫不鎖 commit SHA，原因是這不影響本輪 release 驗收閉環，且會增加後續維護成本。

## 工程師 — 長期經驗

### Release 發佈鏈操作記憶

`publish-release.yml` 建立 GitHub Release 時固定使用 repo secret `GH_PAT`，不要改回 `GITHUB_TOKEN`；用
`GITHUB_TOKEN` 建 release 不會觸發下游 `release-smoke.yml` 的 `release: published` workflow。

`GH_PAT` 設定規格固定沿用上方四項設定（含 Contents read/write 權限）；不要放大 repo 範圍，也不要改 secret 名稱。若 token 過期或被撤銷，
`Verify PAT` 只能檢查非空，實際會在 Step 5 `gh release create` 以 403 失敗；輪替時到 repo Settings →
Secrets and variables → Actions 更新同名 `GH_PAT`。

真實 `v*` tag-push 端到端尚待生產驗證，單元/守護測試為半閉環；目前只能證明
`push tag -> render body -> gh release create 設定 -> release: published smoke 設定` 的結構正確，不代表
GitHub 生產環境 E2E 已實跑過。

本輪不補 `--verify-tag`：現行 workflow 只由 `on.push.tags: v*` 觸發，tag 已存在，且 `Assert tag matches
version` 會比對 `github.ref_name` 與 `v{pyproject_version()}` fail-fast；在未加入 `workflow_dispatch` 手動發佈前，
`--verify-tag` 不需作為驗收必要硬化，避免為重複保護增加範圍。

最小硬化只做一項：`publish-release.yml` 的 `permissions.contents` 維持 `read`，避免給內建
`GITHUB_TOKEN` 不必要的 write 權限；建立 release 所需的 write 權限仍固定由 `secrets.GH_PAT` 承擔。
其他硬化（例如 actions commit SHA 鎖版）本輪不補，因為它不改變 `push tag -> release published ->
release-smoke` 驗收鏈，留待專門供應鏈硬化任務處理。

### 非預期輸出：先懷疑自己的命令，絕不先怪「環境污染」
**慘痛教訓（CI 修復任務）**：我把自己命令的真實後果——`$?` 在錯誤位置沒展開、`>>` append 因我「重試」真的執行了 7 次把 `.gitignore` 寫成 30 行、`{ 多命令 } >> "$R"; cat "$R"` 的交錯——**反复誤判為「pty 污染／串擾」**。一旦貼上「污染」標籤，我就不再相信工具輸出（唯一的事實來源），於是反复重跑、查無謂的 git 歷史、**差點 `git checkout origin/main` 覆蓋檔案**（被使用者打斷）、最後把自己製造的混亂包裝成「環境不可信」甩給使用者決策。**環境從頭到尾完全正常。**

**根因＝外部歸因偏誤**：遇到非預期輸出，第一反應是怪環境，而不是先懷疑自己的命令／邏輯。把自己的 bug 投射成外部故障，然後拒絕相信現實。

固定做法（順序不可顛倒）：
1. **「污染」幾乎永遠是錯的解釋——從解釋庫裡刪掉它**。看到重複行、`$?` 沒展開、錯位，第一假設永遠是「**我的命令寫錯了**」：管線 exit code 取錯位、append 重複執行、複雜結構交錯。
2. **用最簡單的單一命令證偽**：一個 `wc -l file`、一次 `Read` 就能戳破「30 行是污染」的幻覺。你永遠有能力用一條乾淨命令確認真相——先做這個，再下任何結論。
3. **命令要簡單、一次一個目的**：避免 `{ 多命令; 迴圈; heredoc } >> "$R" 2>&1; cat "$R"` 這種結構，它本身製造交錯/重複，正是我誤判的來源。寧可多跑幾條短命令，直接輸出。
4. **不信任 ≠ 可以繞過**：絕不基於「我覺得輸出不可信」去做破壞性操作（覆蓋/reset/push）。不可信就**停下來用簡單命令查清**，而不是繞過事實去賭。
5. **自證對應 + 排除假綠**（仍適用）：輸出的檔名/行號要能回指本次輸入；「全放行」配一個反向黑樣本對照證明真判別力。但這是在「已相信輸出真實」之後的查核，不是把真實輸出當污染的藉口。

### 掃描類腳本的範圍一致性
`pre-commit --all-files` 只掃 **git 追蹤檔**；本地/CI 直接 `bash scan.sh <dir>` 會掃**所有檔含 untracked**。兩端會分歧——untracked 違規檔（如誤放 docs/ 的臨時樣本）會 pre-commit 綠、CI 紅。對策：臨時檔一律放 `$TMPDIR`，不要落在被掃描目錄；驗證收尾用 `git status <dir>` 確認無殘留。

### shell 偵測腳本的可攜性
沿用 rg→grep fallback 範式時，正則限 **ERE**，禁用 lookbehind/PCRE（grep `-P` 非 GNU 環境沒有）。fallback 環境可能連 `sed`/`awk` 都沒有——剝字串改用純 grep（如「白名單優先交替、`grep -oE` 抽片段」），別引入 sed 破壞可攜性。

## 資安審查員 — 長期經驗

從「憑證輪替工作單」資安審查場提煉，作為安全閘門時固定沿用：

1. **審文件型交付也要實跑驗證其安全宣稱，不看文字下結論**。文件若聲稱「`.env` 已在 `.gitignore`」「無 token 明文」，必實 `grep .gitignore`／`grep -E '<token 前綴正則>' <file>` 兌現，避免給人虛假安全感。文件不是程式碼不代表可略讀放行——宣稱要能實查對應。
2. **核可要鎖死範圍，不讓「安全核可」溢出到未審的相鄰攻擊面**。工作單引用了 `docs/token-rotation-runbook.md` 與 `scripts/verify_token_rotation.sh`（`--verify`/`--scan`/`--report`），我只審了 markdown 本身，就明講「核可僅限本文件、不延伸到所引用腳本/runbook」，並把那支腳本列為待另案審查的移交（實跑前需補審指令注入/明文落地）。否則「文件過了」會被誤讀成「整條輪替鏈都安全」。
3. **憑證輪替工作單的資安檢查清單（可複用）**：①先發後撤順序鎖死，新值驗證通過前絕不撤舊（誤撤斷鏈＝403）；②明文絕不進對話/版控/工具輸出，貼證欄位只收 exit code/HTTP 碼/帳號名等非敏感回報；③最小權限 PAT＝本 repo + 僅 `Contents: Read and write` + 設到期日；④人工/AI 分界照「是否接觸明文、是否不可逆帳號操作」切，AI 只做唯讀掃描/報表、不代持明文；⑤驗證指令 `gh auth status` 不可裸跑（驗 keyring 舊值會假綠），`/user` 200 只證身分不證 scope。
4. **`Authorization: Bearer $TOKEN` 的 curl 是低度但真實的明文洩漏點**：token 會在 process args 與 shell history 展開，共用主機 `ps` 可見。優先推環境變數式（`GH_TOKEN=... gh auth status`）；curl 僅列 fallback。此類單人主機下的低度風險列「跟進建議」而非退回理由——聚焦真實風險，不為挑剔而退。

## 向高級工程師學習（跨任務通用的協作習慣）

從與高工的協作中，值得我之後固定沿用：

1. **把錯誤攔在最便宜的階段**。設計評審時就點出不可攜寫法（lookbehind/PCRE 與 fallback 不相容），不讓它流到實作才返工。動工前先確認引擎/邊界假設成立，比寫完再測省得多。
2. **審查不只看配置，要親自實跑行為**。他不靠讀 YAML 下結論，而是親手跑黑/白樣本，因此抓到「同行黑+白」漏報這種純靜態看不出的缺陷。我交付與複核時也要實跑，不靠「看起來對」。
3. **區分「任務範圍內」與「跟進待辦」**。當前接點可核可上線，同時把範圍外的缺陷明確列為移交待辦——不阻擋進度，也不讓問題消失。下結論時把「這條過了」和「但這幾項要記著」分開講清楚。
4. **對自己的判斷也保持懷疑（元認知）**。最關鍵的一點：他承認「這次攔住純屬運氣」，把僥倖提煉成可執行硬規則（驗證須自證對應…），而不是自我安慰。沿用：問「下次同類問題，現有流程能穩定攔住嗎？」若答案是否，就補規則而非賭運氣。
5. **誠實暴露流程缺陷，不粉飾**。「誠實說：不能」比一句「應該沒問題」有價值得多——把不確定與漏洞講出來，團隊才能補。

## PM 主持蒐證/驗證場的經驗（CI 失敗 log 蒐證任務）

從一次「取 CI 失敗 log、記失敗測試名/關鍵訊息/行號」的純蒐證場提煉，PM 派工與驗收時固定沿用：

1. **蒐證派工要把「路徑＋檔名＋自身 sha256」一次釘死**。本場最大返工點：我執行指令只寫死產生器腳本名（`collect_ci_failure.py`），卻沒規定「產出報告的檔名」，於是 engineer 自由命名——口頭宣稱 `CI_FAILURE_REPORT_…md`、實際落地 `run-…authority-report.md`，兩者不符。留任何自由命名空間，必生「宣稱檔名 ≠ 實際落地」分歧。交付物三要素（絕對路徑、確切檔名、報告自身 sha256）在派工當下就講死，不留給執行端自訂。
2. **「不接受口頭宣稱」要靠實查目錄兌現**。engineer 回報完成且列了檔名，但 PM 一跑 `ls` 就發現宣稱檔名不存在。口頭交付＝未驗證；PM 收貨必實 `ls`/`Read`/雜湊實算，光看回報必漏。
3. **PM 驗證雜湊一律「單命令落檔＋Read 讀取」，禁複合管線**。本場我用 `sha256sum … | tail`、`{ 多命令 } ; cat` 等複合結構，自造輸出交錯、誤讀 sha256，浪費數輪。正解：`sha256sum X > out.txt`（單一目的）再用 Read 工具讀 `out.txt`，一次一件事。呼應上方「非預期輸出先懷疑自己命令」。
4. **全工具通道吞輸出＝回傳層故障，不是命令錯，也不是「環境污染」**。當 `printf 'x'`、`echo`、`Read` 一個已知存在的檔、`Glob` 全數回傳空白時，用「最簡命令證偽」即可確認是工具回傳通道故障：既非我命令寫錯（printf 不可能錯），亦非污染幻覺。判別後**立即停止重試探測**（無限探測只燒輪次），改為「據通道故障前已確鑿掌握的事實下判定」，並誠實在結論裡標明「哪幾證因通道故障未能當場閉環、待恢復補做」，不粉飾為「應該沒問題」。
5. **蒐證場多腳本/多中間檔並存時，權威檔要自帶「唯一權威＋應忽略殘檔」聲明**。本場 `.ci-evidence/` 同時有產生器、解析腳本、JSON、failed-only log、authority-report 等多檔；消費端易誤取。權威報告須在檔內聲明唯一權威來源，並列出所有應忽略的中間產物/殘影檔名（含曾出現又被清掉的命名），避免下游取到相反或過期結論。
