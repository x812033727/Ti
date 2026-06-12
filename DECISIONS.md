# 架構決策記錄（ADR）

## 技術選型——純 Python stdlib（asyncio/dataclasses/re/difflib），不引入 AutoGen/LangGraph，借 GroupChat 模式自實作
- 時間：2026-06-13 00:59
- 理由：既有 orchestrator/experts 抽象已等價框架核心，引框架＝砍掉重練，違反任務約束
- 否決方案：引入 AutoGen/AG2 或 LangGraph 作討論層

## 新模組 `studio/discussion.py`，只依賴 flow.py 與 stdlib，嚴禁 import orchestrator；semaphore、broadcast、should_stop 一律建構時注入
- 時間：2026-06-13 00:59
- 理由：防循環依賴，且測試可注入計數型假 semaphore 驗峰值並發
- 否決方案：discussion 直接取用 orchestrator 的 `_llm_semaphore()`/`self._stop`

## 角色介面複用既有 `ExpertLike` Protocol（`speak(prompt, broadcast) -> str`），不新增介面
- 時間：2026-06-13 00:59

## 資料結構——`Mention(speaker, target, stance)`、`Utterance(round, speaker, text, mentions)`、`DiscussionResult(transcript, stop_reason, summary)`；summary 含 `consensus`/`disagreements`/`final_positions`；`stop_reason ∈ {max_rounds, stalled, cancelled}`
- 時間：2026-06-13 00:59

## DiscussionEngine 建構簽名——`(participants: list[tuple[str, ExpertLike]], mode, max_rounds, semaphore=None, broadcast=None, should_stop: Callable[[], bool] | None = None, stall_threshold=0.9)`，入口 `async def run(topic) -> DiscussionResult`；每輪開頭檢查 `should_stop()`，True 即停並標 `cancelled`
- 時間：2026-06-13 00:59
- 理由：補上高工/工程師指出的 stop 傳播缺口，orchestrator 接線時傳 `lambda: self._stop`

## 建構時校驗 participants 名稱唯一、無空白、經 `re.escape` 可安全入 regex；不合法直接 raise
- 時間：2026-06-13 00:59

## parallel 模式＝同輪並行、輪間同步——每輪凍結上一輪 transcript 快照、全員基於同一快照 `asyncio.gather` 發言（每個 speak 包在注入 semaphore 下）、gather 全收齊才寫回＝輪間屏障；寫回與 is_stalled 串接固定依 participants 順序（註解寫明，避免順序抖動誤判相似度）
- 時間：2026-06-13 00:59

## round_robin 模式同輪內依 participants 順序逐一 await，後者可見同輪前者發言
- 時間：2026-06-13 00:59

## context 餵法——議題＋上一輪全員發言＋自己歷史發言（各段截斷上限），不重播全史
- 時間：2026-06-13 00:59
- 否決方案：全員共享完整 transcript（O(N²) token）；RCR 式精細路由留後續

## 反諂媚硬指令內建於 engine prompt 模板：要求 `回應 @角色名: 同意|反對 ＋理由`、「至少指出一個可挑戰點，無異議須說明為何」
- 時間：2026-06-13 00:59

## `parse_mentions(speaker, text)` 的 regex 用 participants 名單組白名單交替（`回應\s*@(?:名1|名2)\s*[:：]\s*(同意|反對)`，名稱 `re.escape`）；target 不在名單或格式不符的行丟棄，整段無合法匹配回空清單
- 時間：2026-06-13 00:59
- 理由：`@(\S+)` 遇含空白名稱即斷；白名單交替是專案既有可攜範式
- 否決方案：通用 `@(\S+)` 捕獲後再過濾

## 收斂三層——max_rounds 硬上限；每輪把「全員發言按 participants 順序串接」append 進 history，用 `flow.is_stalled(history, rounds=2)` 判提前停止；stop_reason 落入 DiscussionResult
- 時間：2026-06-13 00:59

## 小結為規則式、零 LLM 呼叫——共識/分歧由 mentions 同意/反對統計推導，final_positions 取各角色末輪發言；介面預留可選 summarizer 參數供 P1 主持人升級
- 時間：2026-06-13 00:59
- 否決方案：P0 即加主持人 LLM 摘要呼叫

## `_debate()` 分流——`config.DISCUSS_MODE ∈ {round_robin, parallel}` 時組 participants 走 DiscussionEngine；未設或 `legacy` 時原路徑一行不動
- 時間：2026-06-13 00:59

## ADR 蒸餾接縫明確規格——engine 路徑結束後，蒸餾 prompt 餵 `summary.final_positions` 串接＋末輪 transcript（取代舊 proposal/critique 兩變數），沿用同一蒸餾指令與 `adr.record` 落盤；比照 test_adr.py 加一條離線測試
- 時間：2026-06-13 00:59
- 理由：不寫明則實作各寫各的，蒸餾品質不可控

## config——`DISCUSS_MODE = os.getenv("TI_DISCUSS_MODE", "legacy")` 白名單 `{legacy, round_robin, parallel}` 非法值 fallback legacy；`DISCUSS_MAX_ROUNDS` 預設取 `DEBATE_ROUNDS`；**兩欄位必須加入 config.py 的 reload global 區塊**；settings.py 白名單、`.env.example`、ARCHITECTURE.md 同步補
- 時間：2026-06-13 00:59

## 測試隔離——`tests/conftest.py` 統一 `delenv("TI_DISCUSS_MODE")` 防開發機 env 殘留翻轉測試路徑；新測試一律 `monkeypatch.setattr(config, "DISCUSS_MODE", ...)` 不用 setenv
- 時間：2026-06-13 00:59
- 理由：config 是 import 時讀 env，殘留 env 會讓全部既有測試默默改道

## broadcast 不加 per-speaker 標籤——既有 `expert_message` 事件已帶 speaker_key/name，並行交錯下身分不丟
- 時間：2026-06-13 00:59
- 否決方案：仿 `_tagged_broadcast` 加 speaker 標籤層

## 實作前置檢查——確認前端按 speaker_key 聚合 streaming token；若否，parallel 模式下 expert 關 streaming 只發 final
- 時間：2026-06-13 00:59
- 理由：並行 streaming 的 token 級交錯會讓「按最後一筆拼接」的前端花掉

## 移交待辦（不入 P0）——`discussion_round` 事件標籤供前端按輪分組、滾動摘要舊輪次、主持人 LLM 摘要與裁決、真實 LLM API 面驗證
- 時間：2026-06-13 00:59

## 角色檔格式採 Markdown＋YAML frontmatter、一檔一角色（`roles/<key>.md`）；frontmatter 欄位＝現有 Role 欄位（name/avatar/title/model/allowed_tools/permission_mode/tags，key 由檔名推導、寫明則須一致）＋新增 `description`（給未來 moderator 選人）；body 為角色專屬 prompt，載入時自動前置 `_COMMON`
- 時間：2026-06-13 03:26
- 理由：長文 prompt 放 body 最自然、git diff 友善、與 Claude Code subagents 慣例一致；自動前置 _COMMON 免每個自建角色手抄共通守則
- 否決方案：單一大 YAML 檔（多行 prompt 醜、diff 差）；body 含完整 prompt 不前置 _COMMON（漏抄即行為不一致）

## 解析用 PyYAML `safe_load`＋pydantic BaseModel 驗證（`extra="forbid"` 使未知欄位明確報錯），通過後轉 frozen Role；選填預設——model→`config.MODEL_FAST`、allowed_tools→`["Read","Grep"]`、permission_mode→`"default"`（白名單 `{default, acceptEdits}`）；零新依賴（PyYAML 6.0.3、pydantic 隨 fastapi 已在環境）
- 時間：2026-06-13 03:26

## 新增 `studio/role_store.py` 收納載入/驗證/落檔/組隊邏輯，roles.py 僅留內建定義與接面；`role_store.reload()` 為**純同步函式**：先完整 build 好新資料，再於無 await 的同步區塊一次原地變異——`ROSTER[:] =`、`BY_KEY.clear()+update()`、`CORE_ROLES[:] =`，且對被覆蓋的內建 key 一併 `setattr(roles, "PM"/"SENIOR"/...)` 更新具名常數
- 時間：2026-06-13 03:26
- 理由：orchestrator.py:45 等模組級綁定靠原地變異保活；improver.py:308、autopilot.py:387 用具名常數（函式內 import），不 setattr 會出現「同一角色兩種行為」；同步一次變異杜絕並發讀到 clear/update 空窗
- 否決方案：重新賦值模組屬性（既有 import 綁定全失效）；reload 做成 async（引入空窗與重入問題）

## 合併規則明定兩層——`BY_KEY` 含「全部內建（含被 OPTIONAL_ROLES 過濾者）＋全部合法檔案角色」，維持 BY_KEY ⊇ ROSTER 不對稱；`ROSTER` 為內建（同 key 檔案覆蓋後、沿用 OPTIONAL_ROLES 過濾）＋全部新 key 檔案角色；壞檔逐檔拒絕並 log 原因，不影響其他檔與內建
- 時間：2026-06-13 03:26
- 理由：improver.py:386 靠 `key not in BY_KEY` 判斷，BY_KEY 若縮成只含 ROSTER 會默默改變其行為

## reload 語意明寫入文件——進行中 session 已快照 Role 物件，reload 只影響之後建立的 expert，不熱換進行中討論
- 時間：2026-06-13 03:26

## 新增 `config.ROLES_DIR = os.getenv("TI_ROLES_DIR", "<repo>/roles")`，加入 config.py reload global 區塊、settings.py FIELDS 白名單、`.env.example` 同步；測試一律 monkeypatch ROLES_DIR 指向 tmp_path，conftest.py 加 `delenv("TI_ROLES_DIR")` 防環境殘留
- 時間：2026-06-13 03:26

## `/api/roles` CRUD 照 routes.py 既有 pydantic Body＋auth 依賴慣例；GET 每筆帶 `source ∈ {builtin, override, file}` 與 body 原文；POST/PUT 落檔（temp+rename 原子寫）後立即 reload；DELETE：file→刪檔、override→刪檔還原內建、純 builtin→409；key 格式 `^[a-z][a-z0-9_]{1,31}$`，**POST body 與 PUT/DELETE 路徑參數同套驗證**（防路徑穿越）
- 時間：2026-06-13 03:26

## 空殼 persona 驗證規則——對「角色專屬 body 原文（前置 _COMMON 之前）」檢查：去空白後非空，且至少一行匹配 Python re `(輸出|決議|驗證|格式|指令|決策)[:：]`；不符回 422 並指明缺出力格式段落；檔案載入路徑同規則（拒檔記 log）；內建 8 角色定義本身不經此路徑
- 時間：2026-06-13 03:26
- 理由：原 ERE 對內建 engineer（僅「執行指令:」）與 architect（「設計決策:」）body 不匹配，會卡死 override 編輯往返，故擴入「指令｜決策」；驗證在前置 _COMMON 之前，否則 _COMMON 自帶匹配行會讓驗證形同虛設
- 否決方案：規則只對非內建 key 強制（雙軌規則日後難維護，且新角色與 override 應同一標準）

## 加一條守門單測：8 個內建角色 body 全數通過上述驗證規則——防未來改內建 prompt 時 override 往返回歸卡死
- 時間：2026-06-13 03:26

## Group 存單檔 `roles/groups.yaml`（`{name, role_keys[], mode}` 列表），邏輯併入 role_store.py；驗證三硬規則（key 存在於 BY_KEY、不重複、≥2 人）＋mode 白名單 `{round_robin, parallel}`，違反回 422；`/api/groups` CRUD 同 /api/roles 慣例
- 時間：2026-06-13 03:26
- 否決方案：mode 含 legacy（legacy 是無小組的舊路徑，組了小組即無 legacy 語意）

## 範例角色檔放 `roles/_example.md.sample`，載入器只掃 `*.md`，範例不被載入
- 時間：2026-06-13 03:26
- 理由：守住驗收 #1「不放角色檔時行為與現狀完全一致」

## 測試三層——role_store 離線單測（覆蓋內建/壞檔被拒/未知欄位報錯/出力格式驗證/內建 8 body 守門）、API 層 TestClient（CRUD＋各 4xx 案）、冒煙照任務 #5 真實啟動 server 走全流程
- 時間：2026-06-13 03:26

## 移交待辦（不入本輪）——moderator 依 description 自動選角（DyLAN 式）、角色檔 `{variable}` 插值、Web UI 角色編輯器
- 時間：2026-06-13 03:26

