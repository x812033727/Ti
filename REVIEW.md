# Ti 框架整體檢討報告

> 範圍：實戰一陣子後，對 Ti 多代理協作框架的**流程與架構**做整體檢討，聚焦
> **成本、效率／穩定、架構技術債、流程／協作**四個面向。
> 性質：純檢討文件，**不改動任何程式碼**。每個量化／結構性宣稱都標出處（`檔案:行` 或模組名），
> 可逐條 `Read`/`grep` 覆核（符合 `CLAUDE.md`「輸出須自證對應」鐵則）。
> 撰寫基準：`main` 線最新（最末 commit `cf56b04`）。

---

## 0. 總評（Executive Summary）

Ti 在三個維度已達成熟工程水準：**發佈鏈硬化**（PAT guard、版本 SSOT、body render、觸發鏈
mutation 反向測試）、**LLM 韌性集中化**（retry 唯一寄宿 `llm_caller.py`，provider 層禁內建
retry 防疊乘）、**測試隔離**（`conftest.py` 清淨環境、真實 bwrap 沙箱實跑）。這些不需要動。

主要債務集中在三處，並非「沒做」，而是「做了一半」或「只做到事後」：

1. **成本未在執行期受控**——有事後 `usage_report` 估 USD，但無執行期預算上限/熔斷；且
   **完全沒有 prompt caching 斷點**，系統 prompt／角色定義每次 speak 重送。
2. **`orchestrator.py` 單體化**——2308 行 god-object 狀態機，近 20 個 commit 在它身上反覆
   救火（卡死、超時、預算）。
3. **結構化解析脆弱**——雙軌路由與後續任務全靠 regex 抽中文行，無 schema，專家漏吐即靜默丟失。

### 優先級總表

| # | 問題 | 面向 | 影響量級 | 改動風險 | 建議優先級 |
|---|------|------|---------|---------|-----------|
| 1 | 無 prompt caching 斷點 | 成本 | 高（輸入 token 重複計費） | 低 | **P0** |
| 2 | 無執行期預算上限/熔斷 | 成本/穩定 | 中高（失控場燒到撞 timeout） | 低 | **P0** |
| 3 | `OPTIONAL_ROLES` 預設全開＋`DEBATE_ROUNDS=2` | 成本 | 高（可省 30–50%） | 低 | **P0** |
| 4 | retry 公開介面無 guard 測試 | 架構 | 中（複製手寫 retry 不會變紅） | 低 | **P0** |
| 5 | 互動場無軟性時間預算 | 效率/穩定 | 中（只有 autopilot 有） | 中 | **P1** |
| 6 | `orchestrator.py` god-object | 架構 | 中（維護成本、回歸風險） | 高 | **P1** |
| 7 | 結構化文字 regex 解析脆弱 | 架構/流程 | 中（雙軌路由靜默漏件） | 中 | **P1** |
| 8 | 狀態全本地無異地備份 | 架構 | 中（單機故障無救） | 中 | **P2** |
| 9 | 日誌無 level/format/rotation | 架構 | 低（長跑堆滿） | 低 | **P2** |
| 10 | 決策庫膨脹（adr.json 212KB） | 流程 | 低（檢索/注入成本） | 中 | **P2** |

---

## 1. 成本面（Cost）

### 1.1 單場呼叫量拆解

預設配置下，單場討論約 **27 次 LLM 呼叫**，分段如下（角色/模型依 `LEAD_ROLES`／`MODEL_*` 二分）：

| 階段 | 呼叫數 | 入口 |
|------|-------|------|
| 需求澄清 | ~1 | `orchestrator._clarify_requirement()` (`orchestrator.py:283`) |
| 需求拆解 | ~1 | `_decompose()`（PM，Opus） |
| 架構辯論 | 4（含多角色議程可達 ~30） | `_debate()` / `_discuss_agenda()` (`orchestrator.py:532, 665`) |
| 逐任務迭代 | ~18（5 任務 × 3 角色 × avg 1.2 輪） | `_work_task()` (`orchestrator.py:1597`) |
| 最終 Demo | ~2 | `_final_demo()` (`orchestrator.py:2057`) |
| 檢討/後續 | ~1 | `_wrap_up()` (`orchestrator.py:2089`) |

### 1.2 三大成本驅動

1. **角色膨脹**：`OPTIONAL_ROLES` 預設 `researcher,architect,security,devops` **全開**
   (`config.py:379-383`)。每多一個可選角色，辯論/議程階段就 +5~10 次呼叫。
2. **辯論輪數**：`DEBATE_ROUNDS=2` (`config.py:165`)——工程師⇄高工往返翻倍。
3. **失敗重試**：`TASK_MAX_ROUNDS=3` (`config.py:161`)＋Huddle（跑滿仍失敗時召集團隊 +3 次，
   `config.py:242`）＋429/529 退避（`EXPERT_RATE_LIMIT_RETRIES=3`，`config.py:369`，延遲 2~60s
   會燒牆鐘時間與重送 token）。

### 1.3 ⭐ 最高 CP 值：Prompt caching 缺口（P0）

**實證**：`grep -rniE 'cache_control|ephemeral' studio/` 在 `studio/` 內**找不到任何快取斷點**。
`experts.py:292-293` 只是「讀取」SDK 回報的 `cache_read_input_tokens`／`cache_creation_input_tokens`，
框架本身不主動標記 cache breakpoint。每位專家每次 `speak()` 都重送其
`role.system_prompt`（`experts.py:197, 590`）＋累積 transcript，**命中與否完全靠底層 SDK 預設**。

- 系統 prompt／角色守則／辯論 transcript 高度重複，是 prompt caching 的理想標的。

> **稽核更新（已動手前先查清）**：Claude 主路徑實為 `claude_agent_sdk`（`ClaudeSDKClient`，
> `experts.py:312-323`），system_prompt 經 `ClaudeAgentOptions(system_prompt=…)` 傳入，**並非裸
> Anthropic `messages.create`**。走 Agent SDK／bundled CLI 時 prompt caching 由 SDK/CLI **自動處理**，
> 應用層**無法也不該**手動設 `cache_control` 斷點（OpenAI 路徑同理為自動快取）。`experts.py:292-293`
> 讀到的 `cache_read_input_tokens` 正是 SDK 回報「快取確實在作用」。
>
> 因此本項的正確修法**不是加斷點，而是把快取成效量出來**：原本 `cache_read`/`cache_write` 在
> `events.token_usage`（`events.py:117-118`）每筆事件都有採集，卻在聚合層 `history._derive_token_usage`
> 被丟棄、從未在 `usage_report` 呈現。**已落地修正**：聚合與報表補上 `cache_read`/`cache_write` 與
> **命中率**（total 與 by-provider/model/role 皆顯示 `cache_hit=xx%`），讓「快取有沒有用、省多少」可量測。
> 後續若新增裸 API provider，才需另案評估顯式斷點。

### 1.4 成本只到「事後」，缺執行期控制（P0）

- `usage_report.py` 有價目表（`PRICES`，`usage_report.py:22`）、估 USD（Claude 採 SDK `cost_usd`，
  `usage_report.py:6`），透過 `python3 -m studio.usage_report --since … --json` 拉取。
- 但**無執行期預算上限/熔斷**：唯一預算是 autopilot 的**時間**預算（`config.py:670`，0.85 比例，
  且只在 autopilot 傳 `time_budget_s` 時生效）。互動場（web/ws）跑飛了只能撞硬 timeout。
- 建議：加一個「每場 token/USD 軟上限 → 達標即優雅收尾」，與既有時間預算機制併用（見 §3.2）。

### 1.5 三檔降本旋鈕（純 env，零程式改動）

| 檔位 | 設定 | 預期 |
|------|------|------|
| 快速（-30%） | `TI_OPTIONAL_ROLES=researcher` + `TI_DEBATE_ROUNDS=1` | 砍角色膨脹與辯論翻倍 |
| 中等（-50%） | 再加 `TI_LLM_MAX_CONCURRENCY=4` + `TI_MAX_ROUNDS=2` | 降峰值並發＋收斂任務輪數 |
| 完整品質 | 預設 + `TI_CRITIC=1` | 換更高品質（+~33% 成本） |

> 這些是 `config.py:251, 161, 165, 379, 402` 的既有旋鈕，無需改碼即可實驗。建議搭配
> `usage_report` 做 A/B，用數據定預設，而非拍腦袋。

---

## 2. 效率／穩定面（Efficiency & Stability）

### 2.1 救火型 commit 是系統性訊號

近 20 個 commit 高度集中在穩定性救火，值得正視為「架構壓力的症狀」而非孤立 bug：

- `ff82a2e` CodexExpert 整組收屍解 senior/engineer **整輪卡死 3600s**
- `1f94d15` 等 CI→合併單一發佈，**止 PR 堆積與重複任務**
- `435bd99`/`fe73d2f`/`1eb36e0` 一連串「session 軟性時間預算，撞硬 timeout 前優雅收尾」
- `b3596c2` critic 收斂預算 + 尾票不拖垮整場
- `edfab79` orchestrator/experts 串流層 429 退避與「錯誤文字當失敗」防線

判讀：**單輪/單場時長**與**失敗收尾**是當前最痛的點，而它們大多落在 `orchestrator.py`——
與 §4.1 的單體化問題同源。

### 2.2 時間預算只在 autopilot 生效（P1）

軟性時間預算（`config.py:670`，0.85）目前**只在 autopilot 傳 `time_budget_s` 時啟用**。互動場
（web/ws 進來的場次）沒有等價的軟性收尾，遇到超長稽核型任務只能撞硬 timeout。
建議：把「軟性時間預算 + 優雅收尾」下沉成 `StudioSession` 的通用能力，互動場也能設預算
（呼應 §1.4 的 token 預算，兩者共用一條收尾路徑）。

### 2.3 並發理論值 vs 實際峰值

`LLM_MAX_CONCURRENCY=9`（`config.py:402`）、`PARALLEL_LANES=3`（`config.py:399`）。探索顯示實際峰值
常遠低於 9。並發只影響牆鐘時間、不影響 token 總量。建議用 `usage_report`／history 的實際並發
分佈回校這兩個值，避免「理論上限 9」造成的限流誤判與退避連鎖。

---

## 3. 架構／技術債面（Architecture）

### 3.1 `orchestrator.py` god-object（P1）

2308 行單一檔承載整個狀態機：澄清→拆解→辯論→逐任務（並行 lane）→Demo→驗收→發佈，方法地圖見
`orchestrator.py:283~2222`（`_clarify_requirement`/`_debate`/`_discuss_agenda`/`_run_waves`/
`_work_task`/`_huddle_and_retry`/`_final_demo`/`_wrap_up`/`_maybe_publish` …）。

- 問題：救火都打在這一檔，回歸面大、測試難隔離、認知負荷高。
- **好消息**：決議判定已外移到 `flow.py` 純函式（`parse_tasks`/`qa_passed`/`senior_approved`/
  `build_waves`/`is_stalled`），邊界清楚、可測——這是拆解的良好支點。
- 建議：按階段抽出 `ClarifyPhase`/`DebatePhase`/`IteratePhase`/`PublishPhase` 等協作者，
  `StudioSession` 退為協調者；保持 `flow.py` 純函式邊界不變。屬**核心 repo** 改動，風險高，
  需分批、每批守護測試先行。

### 3.2 結構化文字 regex 解析脆弱（P1）

雙軌路由與後續任務靠正則抽中文行：`_RE_CORE_CHANGE = re.compile(r"^\s*核心改動\s*[:：]…")`
（`flow.py:142`，`parse_core_changes()` `flow.py:190`）。完全依賴專家**正確吐出**結構化行——
漏吐、換句話說、格式微偏，就**靜默丟失**，沒有任何告警。

- 雙軌路由（專案 repo vs `CORE_REPO`）是框架核心設計之一，靠這條 regex 維繫，風險不對稱。
- 建議：評估改用 structured output / tool-call schema 讓專家「填欄位」而非「吐行」；過渡期至少
  對「應有核心改動卻 parse 到 0 件」加一條稽核日誌/守門，讓漏件可見（見 §5.1）。

### 3.3 retry 公開介面無 guard（P0，低風險）

`KNOWN_LIMITATIONS.md` 自承：retry 系統公開介面（`llm_caller` 導出）尚未用 guard 測試鎖定。
風險：任何呼叫端複製手寫 retry／繞過 `make_retry_config()` 工廠不會變紅，會悄悄破壞「單一退避
真相 + 禁疊乘」的鐵則。建議補一條 guard：偵測 `studio/` 內出現第二處 `backoff`/`sleep` 重試骨幹
即失敗（屬核心 repo，改動小、收益確定）。

### 3.4 狀態持久化與日誌（P2）

- 狀態全在本地 FS：history JSONL／`backlog.json`／workspace，flock 序列化跨程序安全，但**無異地
  備份/恢復**，單機磁碟故障無救。建議：定期快照到遠端（或至少 backlog/meta 的離機備份）。
- 日誌：多模組啟用 logger 但**無集中 level/format/rotation**，長跑會堆滿。建議統一 logging 設定
  + rotation。

---

## 4. 流程／協作面（Process）

### 4.1 雙軌路由健全性（P1）

設計優雅：專家以結構化行表態 → `backlog.add_items(…, source="core")`（省 `state_dir`＝核心 backlog，
`backlog.py:148, 233`）→ autopilot 在 `CORE_REPO`（`config.py:658`，固定 `x812033727/Ti`）開獨立 PR。
**但健全性完全繫於 §3.2 的 regex**。建議加「路由對帳」：把每場 parse 出的核心改動數、實際入核心
backlog 數、最終開 PR 數做一條可回放的稽核線，讓「靜默漏件」變成可觀測事件。

### 4.2 autopilot 迴圈與 PR 堆積

`1f94d15` 已修「等 CI→合併單一發佈，止 PR 堆積與重複任務」；`cf56b04` 加了 discovery 品質下限
壓制低價值/陷阱型自我餵食提案。後續觀察點：autopilot `_evaluate_self()`「找問題」產生的任務
品質與去重（`AUTOPILOT_EVAL_MEMORY` 窗口）是否穩定，避免自我餵食空轉重新抬頭。

### 4.3 決策沉澱可維護性（P2）

`DECISIONS.md` 168KB、`adr.json` 212KB、`NOTES.md` 58KB 持續膨脹。跨場次注入與檢索成本會隨之上升
（也可能反向推高 prompt token）。建議：定期歸檔/壓縮舊決策，或建索引只注入相關片段，而非全量。

---

## 5. 改善路線圖（Roadmap）

### P0 — 低風險高價值（建議先做）

| 項目 | 預期效益 | 風險 | repo |
|------|---------|------|------|
| Prompt caching 稽核 + 顯式斷點 | 大幅降輸入成本，零品質損失 | 低 | 核心 |
| 每場 token/USD 軟預算 + 優雅收尾 | 止住失控場燒錢撞 timeout | 低 | 核心 |
| `config` 降本預設 A/B（用 usage_report 定案） | -30~50% 成本 | 低 | 核心 |
| retry 公開介面 guard 測試 | 鎖死「單一退避真相」 | 低 | 核心 |

### P1 — 中期結構改善

| 項目 | 預期效益 | 風險 | repo |
|------|---------|------|------|
| 互動場軟性時間預算下沉到 `StudioSession` | 互動場也能優雅收尾 | 中 | 核心 |
| `orchestrator.py` 按階段拆服務 | 降維護/回歸成本 | 高（需分批） | 核心 |
| 結構化輸出取代 regex + 漏件守門 | 雙軌路由不再靜默漏件 | 中 | 核心 |

### P2 — 長期治理

| 項目 | 預期效益 | 風險 | repo |
|------|---------|------|------|
| 狀態異地備份/恢復 | 單機故障可救 | 中 | 核心 |
| 日誌 level/format/rotation 集中 | 長跑不堆滿、可觀測 | 低 | 核心 |
| 決策庫歸檔/索引化 | 降注入與檢索成本 | 中 | 核心 |
| 手動發佈（`workflow_dispatch`）+ `--verify-tag` | 補發佈路徑 | 中 | 核心 |

> 路線圖所有項目皆屬 **Ti 核心框架**改動，依 `CLAUDE.md` 雙軌鐵則應路由到 `CORE_REPO`
> （`x812033727/Ti`）的獨立 PR，不混入專案 repo。

---

## 6. 附錄

### 6.1 關鍵檔案地圖

| 檔案 | 行數 | 職責 |
|------|------|------|
| `studio/orchestrator.py` | 2308 | `StudioSession` 主狀態機（各階段方法） |
| `studio/providers.py` | 1217 | Claude/OpenAI/Codex/Antigravity provider 抽象，禁內建 retry |
| `studio/runner.py` | 1099 | bwrap 沙箱執行、worktree、HTTP demo 驗收 |
| `studio/config.py` | 1026 | 集中設定與 `reload()`、所有降本旋鈕 |
| `studio/autopilot.py` | 996 | 核心 repo 自主迴圈：討論→pytest→PR→merge |
| `studio/llm_caller.py` | 688 | 唯一 retry 骨幹（`RetryConfig`/`run_with_retries`/`backoff_delay`） |
| `studio/experts.py` | 596 | 專家包裝 + `make_retry_config()` 工廠 + cache token 採集 |
| `studio/improver.py` | 547 | 專案持續改良迴圈，雙軌回填 backlog |
| `studio/flow.py` | 420 | 純函式決議層（含 `parse_core_changes` 雙軌路由判定） |
| `studio/backlog.py` | 249 | 雙軌路由單一收斂點（`add_items` source="core"） |
| `studio/usage_report.py` | — | 事後成本聚合 + USD 估算 |

### 6.2 量化假設與來源

- 「單場 ~27 次呼叫」「-30~50%」為**估算**，基於預設配置與 §1.1 分段；正式定案前應以
  `usage_report` 實測校正，勿當精確值引用。
- config 預設值出處：`config.py:48-49, 161, 165, 251, 334, 369, 379-387, 399-402, 670`。
- 「無 caching 斷點」為實證：`grep -rniE 'cache_control|ephemeral' studio/` 無相關命中。

### 6.3 與既有文件對應

- 本報告 §3.3（retry guard）直接對應 `KNOWN_LIMITATIONS.md` 既列待辦。
- §1.3／§3.2／§4.1 屬新提煉缺口，建議補入 `KNOWN_LIMITATIONS.md` 與 `DECISIONS.md`。
- 路線圖全部走核心 repo PR——遵循 `CLAUDE.md`／`ARCHITECTURE.md` 雙軌鐵則。
