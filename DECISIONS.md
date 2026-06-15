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

## 技術選型——零新依賴，沿用行前綴 regex 解析（flow.py 慣例）、broadcast 事件持久化（history JSONL 既有管道），不引入 JSON schema 結構化輸出
- 時間：2026-06-13 05:58
- 否決方案：JSON schema 結構化輸出——與 codebase 行前綴慣例不一致，且既有條列 fallback 已驗證可用

## 模組切分——`flow.parse_agenda` 與 `flow.validate_assignees` 為純函式（離線可測）；orchestrator 只改拆解 prompt 與討論階段呼叫端；discussion.py 與 `parse_tasks_with_deps` 完全不動
- 時間：2026-06-13 05:58

## 介面——子題行 `子題: <標題> | <描述> | <成功準則>`，解析用 `split("|", 2)` 固定最多三段（後段全歸 criteria），缺段允許空；`負責: <role_key>` 緊跟所屬子題行，找不到前置子題時忽略＋log；無 `子題:` 行 fallback 單一子題（原需求全文），零回歸
- 時間：2026-06-13 05:58
- 理由：高工指出標題含 `|` 會錯切，`split("|", 2)` 一行解決，單測加此樣本

## 分派硬驗證——合法集合＝本場實際出席角色 keys（experts dict）而非全域 BY_KEY；非法/缺漏 fallback 順序：`engineer` 若在出席集合，否則取第一個出席者（`fallback if fallback in available_keys else next(iter(available_keys))`），修正記錄入 log 與議程事件
- 時間：2026-06-13 05:58
- 理由：工程師與高工同點名 fallback 硬編 engineer 在自訂角色組合下自身就是非法 key；純函式不得依賴呼叫端保證
- 否決方案：fallback 永遠 engineer——engineer 缺席時 fallback 結果仍非法，硬驗證形同虛設

## assignee 消費端——逐子題討論階段由 orchestrator 讀取：該子題的提案方（先發言者）＝assignee 對應 expert，並在 topic 文字標明「主責: <角色名>」；同時隨 agenda_plan 事件持久化供重看
- 時間：2026-06-13 05:58
- 理由：高工正確指出無消費端就是死資料；先發言權是不改引擎介面下最小且有實效的消費方式

## 持久化——events.py 新增 `EventType.AGENDA_PLAN` enum 成員＋`agenda_plan(session_id, agenda, tasks, assignments)` 建構子，經既有 broadcast→record_event 入 history；meta.json 與 history.py 不改；實作清單加一項：確認 web 前端對未知 event type 容錯忽略，會炸則補 default case
- 時間：2026-06-13 05:58
- 理由：工程師實跑確認 type 是 enum，只加函式不夠

## ADR 蒸餾收斂為一次——逐子題只跑討論、收集各子題結論串接成單一 design_note，討論全部結束後做一次蒸餾、一筆 adr.record、一次 commit
- 時間：2026-06-13 05:58
- 理由：工程師實測指出逐子題各帶蒸餾＋commit 會 ×N 成本，且後續子題 adr.context 吃到前面子題決策造成干擾；高工同問聚合方式——統一收斂為一次最便宜且語意正確
- 否決方案：每子題各自蒸餾＋commit——2–5 倍 token/時間成本、ADR 碎片化、子題間 context 互相污染

## 成本上界——解析端硬上限子題數 5（超出截斷並 log，prompt 的 2–5 只是建議不是防線）；多子題時每子題討論輪數走新 config `TI_AGENDA_ROUNDS`（預設 1），總成本上界＝5×1 輪可控
- 時間：2026-06-13 05:58
- 理由：高工指出 prompt 約束不可當防線；5 子題 × 既有 DEBATE_ROUNDS 會讓架構階段成本失控

## 粒度守則入拆解 prompt——「子題 2–5 個、每任務一句可驗收、探索型允許單子題不硬拆」字面寫入，可 grep 驗證
- 時間：2026-06-13 05:58

## 測試切分——flow 純函式單測（合法/非法 key、fallback 鏈含 engineer 缺席 case、標題含 `|`、單子題 fallback、超量截斷）＋8 角色 description 非空守門單測＋fake PM 腳本（含一合法一非法 key）流程測試＋#5 真實 server 冒煙特別驗證 PM 對疊加格式的遵循率，輸出回指輸入排除假綠
- 時間：2026-06-13 05:58
- 理由：工程師提醒拆解單次呼叫已產多種格式，再疊 `子題:`/`負責:` 格式負擔不小，fallback 路徑須在真實冒煙實測到

## `parse_conclusion` 純函式置於 `flow.py`，沿用既有 `re.match(r"^\s*<標籤>\s*[:：]\s*(.+)$", line)` 行前綴範式＋全形冒號容錯，解析 `共識:／分歧:／未決:／行動:` 四前綴，回傳 `{"consensus":[], "disagreements":[], "open_questions":[], "actions":[]}` 結構化 dict
- 時間：2026-06-13 10:44
- 理由：flow.py 已有 7 處同款 parser，Python `re` 直接複用零風險
- 否決方案：另起 JSON schema 或新解析範式——與既有慣例不一致、徒增負擔

## 四前綴全缺時 `parse_conclusion` 回空骨架（四鍵皆空 list）而非拋例外，由呼叫端偵測空骨架走 fallback，對齊 `adr.parse_adr` 「失敗即降級」
- 時間：2026-06-13 10:44

## `discussion._build_summary` 既有三鍵 `consensus/disagreements/final_positions` 維持原扁平 `agree-disagree` set 邏輯完全不動，防回歸（驗收 #2）
- 時間：2026-06-13 10:44

## 新增 `open_questions` 鍵採 **per-pair 末輪 stance 判定**：對每個 `(speaker,target)` 取最大 `Utterance.round` 的末態 stance，末態為「反對」者列入 open_questions；明確禁止沿用扁平 agree/disagree set 推「未轉同意收斂」
- 時間：2026-06-13 10:44
- 理由：高工指出扁平 set 會讓「先同意後反對」末態仍反對者被 `disagree-agree` 誤排除、漏判未決；唯有取 per-pair 末輪 stance 才正確，這是設計合約層級漏洞，現在釘最便宜
- 否決方案：工程師的純集合 `disagree-agree`——無法處理「先同意後反對」末態反對之 case，會漏判未決

## `unique_findings` 定義為 **role 粒度**——target 從未被任何 speaker mention 的角色發言（無人回應者）；建構 mention 圖時排除 self-mention（僅計 `m.speaker != m.target`），並補 self-mention 黑樣本測試防假陰性
- 時間：2026-06-13 10:44
- 理由：工程師指出 self-mention 會讓角色誤判為「被回應」而漏掉 unique；同時標明此為角色粒度近似、非論點粒度遺漏偵測，避免日後誤用

## `consensus` 維持僅取明確 `stance=同意` 的 mention，無 mention 的發言一律歸入 `unique_findings` 不進 consensus，以區分「明確同意」與「無人表態」
- 時間：2026-06-13 10:44
- 理由：工程師確認此為結構保證非測試運氣——零-mention transcript 的 `agree` 必為空集，假共識無生成路徑

## 測試須含零-mention 黑樣本（角色全無 mention）＋**灰樣本（角色被部分 mention）**，確認 unique/open_questions 語意，避免判別力只驗極端一半
- 時間：2026-06-13 10:44
- 理由：高工指出零-mention 只驗極端，灰樣本才驗到 role 粒度的真實語意

## 新增 `conclusion.py` 模組（對齊 `adr.py`），職責＝組 prompt＋接 senior one-shot＋呼叫 `parse_conclusion`＋fallback＋render markdown＋落盤，介面 `summarize(summary, transcript) -> dict` 與 `record(cwd, conclusion, *, session_id) -> Path`
- 時間：2026-06-13 10:44

## 結論蒸餾與 ADR 蒸餾分開兩次 senior one-shot 呼叫、不合併
- 時間：2026-06-13 10:44
- 理由：輸出格式（`共識:/分歧:/未決:/行動:` vs `設計決策:`）與職責不同，混一個 prompt 降遵循率；兩者皆×1 成本可控
- 否決方案：合併成單次 senior 呼叫——省一次呼叫但拉低各自格式遵循率，不划算

## 蒸餾 prompt 含三條防坑硬指令——①只彙整 transcript 出現過的論點、不得新增②無人反對≠共識，需區分明確同意與無人表態③強分歧須保留並標明雙方，字面寫入可 grep 驗證
- 時間：2026-06-13 10:44

## 蒸餾 prompt 要求每條結論盡量帶 `(round, speaker)` 錨點，但真錨點事實來源為規則層 summary（`final_positions`/`unique_findings` 天生帶 speaker），不信任 LLM 自填錨點；驗收「至少一條回指 transcript」由規則骨架保證
- 時間：2026-06-13 10:44

## `CONCLUSION.md` 每場覆寫式單檔、落 workspace 根，四段固定 `## 共識／## 分歧／## 未決事項／## 後續行動`，歷史保存靠 git commit 而非 append 累積
- 時間：2026-06-13 10:44
- 否決方案：學 adr.py append 累積——結論是本場快照，累積語意錯且會膨脹；多檔歷史回顧留待 M2

## fallback 路徑——`parse_conclusion` 回空骨架時 `conclusion.record` 改用規則式 summary 骨架 render markdown：consensus→共識、disagreements→分歧、open_questions→未決；**行動段留空並標「（蒸餾失靈，無行動項）」，不以 final_positions 末輪發言冒充 action**
- 時間：2026-06-13 10:44
- 理由：高工指出末輪發言不是行動項，硬塞語意偏差；fallback 仍須產出 CONCLUSION.md（驗收 #6）但不偽造行動

## `events.py` 新增 `EventType.CONCLUSION`＋`conclusion(session_id, path, summary)` 建構子，沿用既有 broadcast→record_event 入 history 管道；同步確認 web 前端對未知 event type 有 default 容錯，會炸則補 default case
- 時間：2026-06-13 10:44

## orchestrator 接線於 `_discuss_agenda` 討論全部結束後、ADR 蒸餾同階段，依序 summarize→record→commit（訊息「結論彙整：產出 CONCLUSION.md」）→broadcast CONCLUSION 事件，單一接點不散落
- 時間：2026-06-13 10:44

## 落盤 commit 沿用既有 `self._commit` 慣例，不引入 `fcntl` 鎖
- 時間：2026-06-13 10:44
- 理由：單檔每場覆寫一次、無同檔跨程序併發，鎖為過度設計（adr.py 的鎖是為 read-modify-write 累積＋跨場併發）

## 移除原設計「ERE 可攜性」措辭——此處為 Python `re` 非 shell grep fallback，CLAUDE.md 的 lookbehind/PCRE 教訓不適用，`[:：]` 全形容錯沿用既有 parser 即可
- 時間：2026-06-13 10:44
- 理由：高工澄清，避免把掃描腳本的可攜性顧慮誤套到 Python 解析

## 測試切分——`parse_conclusion` 四段正常解析／全形冒號／漏標回空骨架純函式測試；`_build_summary` 新鍵（含 per-pair 末輪 stance、self-mention 排除、零-mention 黑樣本、部分-mention 灰樣本）stance 路徑測試；離線 e2e 驗證跑完 workspace 根產出 CONCLUSION.md 四段且至少一條回指 transcript（自證對應、排除假綠）
- 時間：2026-06-13 10:44

## 任務#1 第④條自我校驗指令插入 `build_prompt` 的 159 行（硬指令③）與 160 行（「盡量帶 (round, speaker) 錨點」提示）之間
- 時間：2026-06-13 12:23
- 理由：經兩位親查核實該位置不動四鍵前綴格式，`flow.parse_conclusion` 解析不破壞（驗收#1）

## 任務#1 第④條措辭須與 160 行「盡量帶錨點」語意對齊不打架——統一為「能對應骨架錨點者帶上，自檢確認每條都有骨架依據，查無依據者刪除」，避免 LLM 將「盡量帶」與「無依據即刪」理解成衝突
- 時間：2026-06-13 12:23
- 理由：工程師指出兩行語氣若對立會擾動 LLM 輸出；收斂為同一條「有依據才留、留則帶錨」的一致敘事

## 任務#2 護欄套用範圍以「是否走 `_anchored_from_summary` 回填」為判別，凡未被回填的 LLM 自產非空鍵一律過護欄，明確涵蓋 `actions` 鍵
- 時間：2026-06-13 12:23
- 理由：工程師指出 `actions` 永遠走 LLM 原文、不在部分漏標回填範圍內，若護欄只列 consensus/disagreements/open_questions 則幻覺 action 漏網
- 否決方案：寫死鍵名清單套護欄——會遺漏 actions，且與「回填條目不重複處理」的判別邏輯不一致

## 任務#2 護欄 `_guard_anchor` 採「抽 `(R<n> <speaker>)` token + 驗 speaker 存在性」保守判別，抽不到或 speaker 不存在於 transcript 才加 `（未錨定）`
- 時間：2026-06-13 12:23
- 理由：LLM 自由文字無法用規則層 `f"{s} {verb} {t}"` 精確重建，保守策略寧漏標不誤傷真錨點；`re` 限 ERE 等價、不用 PCRE 符合 CLAUDE.md 可攜性鐵則
- 否決方案：規則層精確重建比對——僅適用規則條目，套到 LLM 自由文字會大量誤標真來源條目

## 任務#2 須在程式註解與 #5 交付說明標明護欄判別力上限為「只驗 speaker 出現、不驗 round 與論點對應」的盡力而為，非幻覺攔截保證，列為已知待辦
- 時間：2026-06-13 12:23
- 理由：高工指出「真 speaker＋幻覺論點」仍會漏標；須避免驗收#2 被誤解為護欄能保證攔幻覺，誠實暴露限制（CLAUDE.md 元認知鐵則）

## 任務#3 `record` 雙寫採「md 主檔先寫保底、sidecar best-effort 後寫」語義——`conclusion.json` 寫入包 try，失敗降級為只保留 `CONCLUSION.md` ＋ log warning，不拋例外、不拖垮人讀主檔與既有 record→commit→broadcast 時序
- 時間：2026-06-13 12:23
- 理由：高工指出 `_record_conclusion` 無 try，sidecar 拋例外會中斷 commit/broadcast 且 md 已落卻未入 git；md 為驗收核心（#3/#5/#6）必落保底，sidecar 為 M2 前瞻附屬可降級
- 否決方案：工程師「sidecar 先寫、md 後寫代表整組就緒」——順序技巧仍需包 try 才安全，且讓附屬檔擋在主檔前不符主檔優先級；改以「主檔先落＋附屬 best-effort」更穩

## 任務#3 sidecar 寫入失敗的降級路徑須清理殘留 `.json.tmp`，收尾以 `git status` 確認 workspace 無未追蹤殘檔
- 時間：2026-06-13 12:23
- 理由：高工提醒 tmp-replace 失敗可能殘留 `.json.tmp`，沿用 `adr.py` 範式並符合 CLAUDE.md「臨時檔不落被掃描目錄、收尾 git status 驗無殘留」鐵則

## 任務#3 sidecar schema 為 `{"version": 1, "session_id", "rounds", "consensus", "disagreements", "open_questions", "actions"}`，`record` 加 `rounds: int = 0` keyword-only 參數承載輪數，回傳值維持 `CONCLUSION.md` path 不變
- 時間：2026-06-13 12:23
- 理由：`version` 欄供 M2 演進辨識；回傳不變則 broadcast CONCLUSION 事件零影響；`cwd is None` 時兩檔皆不落、回 None（驗收#3）

## 任務#4 orchestrator `_record_conclusion` 僅改一行——`conclusion.record(...)` 傳 `rounds=max((u.round for u in transcript), default=0)`，commit 範圍不需改動
- 時間：2026-06-13 12:23
- 理由：高工驗實 `git_commit` 為 `git add -A`（runner.py 605），sidecar 在 commit 前由 record 寫出即同 commit 入 git，#4 零接線假設成立（驗收#4）

## 任務#4 維持既有 record→commit→broadcast 時序與單一接點不變，sidecar 僅為 record 內多寫一檔，不新增接點、不散落
- 時間：2026-06-13 12:23

## 任務#5 測試切分——`build_prompt` 第④條 grep＋前綴不破壞；`_guard_anchor` 純函式測（有效錨點／真 speaker＋幻覺論點／無錨點三類，含高工指定黑樣本）；`record` 雙寫 JSON 合法性＋四鍵＋session_id＋rounds＋`cwd=None` 雙不落；sidecar 寫失敗降級只保 md 的路徑測試
- 時間：2026-06-13 12:23
- 理由：護欄黑樣本須涵蓋「真 speaker＋幻覺論點」以驗判別力邊界並佐證已知限制

## 任務#5 離線 e2e 須實跑「全員無反對」與「LLM 漏標前綴 fallback」兩條既有路徑，確認四段產出＋至少一條回指 transcript 無回歸，且第④條與 `_guard_anchor` 以實跑黑白樣本驗證、不只 grep
- 時間：2026-06-13 12:23
- 理由：兩位均強調 CLAUDE.md 鐵則「親自實跑、自證對應、排除假綠」，prompt/格式改動不靠讀碼下結論

## 全程不砍既有架構——規則為骨、LLM 為肉、覆寫式單檔＋git 快照、md 人讀／json 機讀雙寫不變；三項皆低風險增量，#1/#2/#3 同檔不同函式可並行，#4 待 #3 sidecar 路徑定後接線，#5 收尾
- 時間：2026-06-13 12:23

## 任務#1 僅確認 `providers.py:107–108`（make_expert openai 分支）與 `providers.py:149`（complete_once 呼叫 make_expert）兩處行號，**零程式碼改動**，鏈路已閉合
- 時間：2026-06-14 16:05

## 所有新測試追加進 `tests/core/test_providers.py` 尾端，複用既有 `FakeChat`/`_msg`/`_tc`，不新增測試檔
- 時間：2026-06-14 16:05

## **所有 config 屬性異動一律用 `monkeypatch.setattr(config, "屬性名", 值)`，禁止直接賦值**，確保測後自動還原，不污染後續測試
- 時間：2026-06-14 16:05
- 理由：config 是模組級全域；直接賦值無還原機制，PROVIDER/OFFLINE_MODE 會洩漏，任務#5 補跑雖能抓但原因難追；現有 line 85 已有正確示範，新測試須一致

## 注入點選 `monkeypatch.setattr(providers, "_openai_chat", fake_chat_instance)`
- 時間：2026-06-14 16:05
- 理由：`make_expert` 在 call-time 才取 `_openai_chat`（非 import-time 閉包），patch 模組屬性確實截斷真實外呼，且比 patch SDK 內部更穩定

## **成功路徑測試（任務#2）** 需設定以下四項 monkeypatch：`PROVIDER="openai"`、`OFFLINE_MODE=False`、`OPENAI_BASE_URL="http://local"`（令 provider_ready() 走 openai 分支且回 True）；傳 `timeout=1.0`；斷言回傳等於注入文字且 `fake.seen` 長度為 1
- 時間：2026-06-14 16:05

## 成功路徑注入 `_msg(content="文字", tool_calls=None)` 並斷言 `speak` 首回合收斂（`seen` 長度 1）；若實跑發現 speak 有額外包裝導致斷言失敗，須對應調整，**以實跑結果為準，不靠讀碼猜測**
- 時間：2026-06-14 16:05
- 理由：工程師指出「純 content 首回合收斂」是未在設計中明寫的假設，須實跑自證，符合 CLAUDE.md「親自實跑、自證對應」鐵則

## **短路守門三態（任務#3）** 各自一支測試，每支均裝 FakeChat spy 並斷言 `fake.seen == []` 作反向對照排假綠；三態分別為：① `cwd=None`（其餘 config 無須特設）、② `OFFLINE_MODE=True`＋`PROVIDER="openai"`＋`OPENAI_BASE_URL="http://local"`、③ `PROVIDER="openai"`＋`OPENAI_API_KEY=""`＋`OPENAI_BASE_URL=""`
- 時間：2026-06-14 16:05

## **`provider_ready()=False` 守門測試（第三態）必須同時設 `PROVIDER="openai"`**，否則函數走 claude 分支不讀 OPENAI_* 變數，guard 驗的是 CI 有無 claude 憑證，非目標行為
- 時間：2026-06-14 16:05
- 否決方案：僅設雙空字串不設 PROVIDER——高工確認 `config.py:621` 先判斷 PROVIDER 分支，不設 openai 會靜默走錯分支，假綠/假紅均有可能

## **例外降級測試（任務#4）** 用 inline `async def exploding_chat(...): raise RuntimeError("API 炸了")`，同時設 `PROVIDER="openai"`＋`OFFLINE_MODE=False`＋`OPENAI_BASE_URL="http://local"` 讓 guard 通過，斷言 `complete_once` 回 `""` 且不外拋
- 時間：2026-06-14 16:05
- 否決方案：新建 ExplodingChat class——inline async def 已足夠覆蓋 `except Exception: return ""` 路徑，無需增加 class

## 任務#2/#3/#4 三組測試互不依賴，可並行撰寫；任務#5 排最後，執行 `.venv/bin/python -m pytest tests/core/test_providers.py -q` 確認全綠、無真實網路 I/O
- 時間：2026-06-14 16:05

## `RetryConfig` dataclass 新增於 `llm_caller.py`，緊鄰 `run_with_retries`，欄位為 `max_retries: int`、`backoff: Callable[[float|None, int], float]`、`sleep: Callable[[float], Awaitable[None]]`
- 時間：2026-06-14 20:00
- 理由：llm_caller 是 `run_with_retries` 介面的所有者，RetryConfig 是其參數的結構化型別；零 config 依賴確保模組維持 provider-agnostic
- 否決方案：放 `experts.py` — 會讓下游 import `experts` 才能取得型別，破壞 llm_caller 作為穩定公開介面的角色

## `RetryConfig` 提供 `as_kwargs() -> dict` 方法，明確回傳 `{"max_retries": self.max_retries, "backoff": self.backoff, "sleep": self.sleep}`，docstring 標注「僅封裝 config-driven 三參數，call-site callback 鉤平鋪傳入」
- 時間：2026-06-14 20:00
- 理由：Callable 欄位無法 `dataclasses.asdict()` 序列化；docstring 說明消除後來者對「其餘 6 個 kwargs 為何未封裝」的困惑（高工意見）
- 否決方案：`dataclasses.asdict()` — Callable 欄位崩潰；`**cfg.__dict__` — 繞過 dataclass 抽象且語義不清

## `make_retry_config()` 落點 `experts.py`，call-time 讀 config，回傳 `RetryConfig(max_retries=max(0, config.EXPERT_RATE_LIMIT_RETRIES), backoff=_backoff_delay, sleep=_sleep)`
- 時間：2026-06-14 20:00
- 理由：讀 config 的責任屬消費層；工廠在 `experts.py` 保持 `llm_caller` config-free
- 否決方案：放 `config.py` — config 模組不應 import experts 的 Callable；放 `llm_caller.py` — 違反 provider-agnostic 原則

## `max(0, ...)` clamp 移入 `make_retry_config()` 內部，`RetryConfig.max_retries` 保證 ≥0；移除 `_speak_with_retries` L380 的本地讀值
- 時間：2026-06-14 20:00
- 理由：工程師指出 clamp 語義不可在「移除本地變數」時靜默丟失；高工補充 `run_with_retries` 雖在 L410 也有 `max(0, ...)`，但在工廠明訂讓外部合約清晰、防呆在最近端

## `_speak_with_retries` 改動點：① 刪 L380 本地 `max_retries` 讀值；② 插入 `cfg = make_retry_config()`；③ `run_with_retries` 呼叫改傳 `**cfg.as_kwargs()`；④ L405/L407 fallback 字串的 `{max_retries}` 改為 `{cfg.max_retries}`
- 時間：2026-06-14 20:00
- 理由：工程師與高工同步指出 L405/L407 仍引用局部變數，改後若不替換即 NameError；此為實作必補項，納入最小化改動清單

## `_backoff_delay`、`_sleep` 保留為 `experts.py` 模組級 lazy 函式，`make_retry_config()` 直接以函式物件引用傳入 `RetryConfig`；不在工廠內另建 closure
- 時間：2026-06-14 20:00
- 理由：兩者已是 call-time 讀 config 的 lazy closure（L83-91 既有範本），直接引用等價；不重複造 closure 避免多層包裝增加可讀性負擔

## wiring 測試用 `mocker.spy(experts, "make_retry_config")`（保留真實邏輯），斷言 `spy.assert_called_once()`；補反向對照：`monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 7)` 後再呼工廠，斷言 `cfg.max_retries == 7`
- 時間：2026-06-14 20:00
- 理由：spy 不替換實現，可同時驗「有取用」與「值正確流入」；反向對照排假綠（高工確認）
- 否決方案：`mocker.patch("make_retry_config")` 直接替換 — 只驗呼叫、跳過真實值流，無法確認 config 值是否正確傳入

## 任務#5 grep 驗收對象為 `max_retries` 與 `_backoff_delay` 的所有引用點，確認無第二套散傳退避呼叫點，且 L405/L407 已改為 `cfg.max_retries`（工程師建議：grep max_retries 引用點可順帶抓出此細節）
- 時間：2026-06-14 20:00

## `providers.py` 頂層直接 `from .experts import make_retry_config`，不新增模組、不複製工廠。
- 時間：2026-06-14 21:01
- 理由：experts.py 無任何 providers import（循環依賴不存在），共用同一 `EXPERT_RATE_LIMIT_*` 旋鈕保持三端一致。

## `OpenAIExpert.speak()` 將整個工具迴圈（`for _ in range(OPENAI_MAX_STEPS)`）打包為單一 `_attempt`，交 `run_with_retries` 控制退避。
- 時間：2026-06-14 21:01
- 理由：對齊 Claude 端「一次 attempt = 一個完整 turn」語義；逐步包裝需在上層處理 `resp` 型態歧義，複雜度不值得。
- 否決方案：逐 `_chat` 單步包裝（複雜、需把 resp 從 attempt_fn 回傳再由外層繼續處理，架構破碎）。

## 工具副作用冪等性為**隱性合約**，選擇接受（方案 a）；但 `_attempt` 函式上方 comment 必須明文標注「retry 假設工具呼叫冪等，寫入型工具不在此重試保證範圍；非冪等工具須在 tools.execute 層自行防護」。
- 時間：2026-06-14 21:01
- 理由：429 多發生在 `_chat` 呼叫階段；工具執行本身不觸發限流；重放機率低但必須讓維護者知道這個前提。
- 否決方案：方案 b（只包 `_chat` 的逐步退避）——回到複雜的多層包裝，首次 rate_limit 發生在工具執行後時仍需 rollback 邏輯，代價更高。

## `speak()` 進入時 `snapshot = list(self._messages)`；user 訊息 append 與 `collected = []` 搬入 `_attempt` 內部；retry 時以 `self._messages[:] = snapshot + [user_msg]` 恢復，確保訊息不重複累加。
- 時間：2026-06-14 21:01

## `idle` broadcast 置於 `run_with_retries` 呼叫外的 `finally` 塊，覆蓋成功、限流耗盡、api_error、unknown exception 四路徑。
- 時間：2026-06-14 21:01
- 否決方案：將 idle 保留在 `_attempt` 末尾——rate_limit_exhausted 路徑不經 `_attempt` 正常結束，會漏播 idle。

## `on_retry` hook 廣播 `expert_status("thinking")` 並 `logger.warning`，鏡像 Claude 端 `_on_retry` 行為。
- 時間：2026-06-14 21:01

## `on_rate_limit_exhausted` 與 `on_api_error` 皆 `return ""`；logger.warning 必須包含 expert name、exception snippet、已重試次數，確保排查有跡可循。
- 時間：2026-06-14 21:01
- 理由：高工指出空字串與真實空回應在上層無法區分，詳細 log 是唯一補救手段。

## `classify_failure(exc)` 作為 OpenAI 例外分類的唯一入口；wiring 測試中必須加一條直接斷言 `classify_failure(fake_openai_rate_limit_error)` 的行為——若 `retry_after` 為 None 則明文標注走固定退避路徑（可接受），若能解析則斷言非 None；禁止「假設能命中」而不驗。
- 時間：2026-06-14 21:01
- 理由：高工標為 🔴 高風險——`classify_api_text` 原設計針對 Claude 格式，OpenAI RateLimitError 字串格式差異若導致命中失敗，會靜默退化為預設退避而非 retry-after 優先，難以察覺。

## `providers.py` 層不自行解析 `retry_after`，零修改 `llm_caller.py`。
- 時間：2026-06-14 21:01

## `complete_once()` 改動極小——`except Exception: return ""` 保留（永不 raise 合約不變），加 comment 說明「429 已由 speak() 內部 run_with_retries 吸收，此處 except 僅兜底未知錯誤」；不在 complete_once 層套第二層 run_with_retries。
- 時間：2026-06-14 21:01
- 否決方案：complete_once 套外層 run_with_retries（雙層重試，且與 speak() 內部退避疊加語義不清）。

## wiring 測試四項必測——① `mocker.spy(experts, "make_retry_config")` 斷言 called_once；② `monkeypatch EXPERT_RATE_LIMIT_RETRIES=7` 後斷言 `run_with_retries` 實收 `max_retries=7`（反向對照排假綠）；③ `classify_failure` 對偽造 OpenAI RateLimitError 的行為顯式斷言；④ 重試耗盡時 speak 回傳 `""` 且不含核可關鍵詞（工程師指出的 partial broadcast 不一致風險）。
- 時間：2026-06-14 21:01

## 零新外部依賴（無 tenacity）、零新 env 變數；三端統一走 `EXPERT_RATE_LIMIT_*` 旋鈕。
- 時間：2026-06-14 21:01

## `RetryConfig` 新增三 field：`base: float = DEFAULT_BACKOFF_BASE`、`cap: float = DEFAULT_BACKOFF_CAP`、`jitter: float = DEFAULT_BACKOFF_JITTER`，`backoff` 改為 `Callable | None = None`；欄位順序：`max_retries`（唯一必填）→ `base/cap/jitter/backoff/sleep`（均有預設值）。
- 時間：2026-06-14 21:50
- 理由：dataclass 必填在前、選填在後，無重排衝突；現有唯一生產呼叫點（`experts.py:116`）全程 keyword，grep 已確認零位置參數地雷。
- 否決方案：`kw_only=True`——Python 3.10+ 限定，且現有已全 keyword，多此加限制。

## `__post_init__` 對非法輸入先 `warnings.warn`（`stacklevel=2`）再 silent clamp，不拋例外；四條防線：`cap <= 0 → DEFAULT_BACKOFF_CAP`、`base <= 0 → DEFAULT_BACKOFF_BASE`、`max_retries < 0 → 0`、`jitter` 超 [0,1] → `max(0.0, min(1.0, ...))`。
- 時間：2026-06-14 21:50
- 理由：高工要求至少 `warnings.warn`——讓呼叫方錯誤輸入在 log 留跡，但不破壞執行路徑；`base=0` 對 529 路徑產出 0 延遲（thundering herd），納入防線。
- 否決方案：`raise ValueError`——破壞「不拋例外」的既有合約語意，且 clamp 已保安全；否決 pure silent clamp——高工指出生產環境 debug 代價太高。

## `__post_init__` 自動生成 backoff 時，先取本地變數再捕捉，**不捕捉 `self`**：`_b, _c, _j = self.base, self.cap, self.jitter`，再 `lambda ra, att: backoff_delay(ra, att, base=_b, cap=_c, jitter=_j)`。
- 時間：2026-06-14 21:50
- 理由：捕捉 `self` 會讓事後 mutate 屬性靜默改變退避行為；本地變數捕捉在 clamp 完成後固化，語意清晰且無副作用。
- 否決方案：`frozen=True`——`__post_init__` 內 `self.backoff = ...` 需改 `object.__setattr__` 繞過凍結，程式碼可讀性損失大於收益；否決文件標註「視為不可變」但不修閉包——未消除工程師指出的真實風險。

## `RetryConfig` 加 docstring 一行：「`base/cap/jitter` 於建構後視為不可變——更改屬性不影響已生成的 `backoff` callback。」
- 時間：2026-06-14 21:50
- 理由：技術上仍可 mutate（不加 frozen），故需文件警語守住語意邊界，防止未來維護者誤用。

## `__post_init__` 末尾才判斷 `if self.backoff is None`（clamp 全部完成後），`backoff` 顯式傳入時跳過生成，不覆蓋。
- 時間：2026-06-14 21:50
- 理由：此路徑服務 `experts.make_retry_config`（傳 `backoff=_backoff_delay`）——顯式注入優先是設計契約，wiring 測試 L176 以 `is experts._backoff_delay` 斷言守門。

## `experts.make_retry_config` 不遷移，繼續傳 `backoff=_backoff_delay, sleep=_sleep`；`_backoff_delay` 保留為 lazy config-read 錨點。
- 時間：2026-06-14 21:50
- 理由：遷移會讓 `test_make_retry_config_wiring_qa.py:176` 的 `is _backoff_delay` 斷言失守；且 lazy read（retry 時才讀 config）語意優於 construction-time 快照——兩路並行，各有適用場景。

## `as_kwargs()` 不修改，繼續 export `{max_retries, backoff, sleep}`；`__post_init__` 後 `backoff` 保證非 None，無需 None guard。
- 時間：2026-06-14 21:50

## 測試補強五條——① `jitter=0` 黑樣本（`rand=lambda:1.0`，確定值不受隨機源影響）；② `jitter=0.25, rand=lambda:1.0` 白樣本（429 路徑不早於 retry_after、529 路徑抖動到下界）；③ clamp 邊界四條（`cap=0`、`base=0`、`max_retries=-1`、`jitter=1.5`）各加 `pytest.warns` 確認 warning 有發；④ 顯式 `backoff=<fn>` 注入後 `__post_init__` 不覆蓋；⑤ 既有 `_backoff_delay` 測試（L407–438）零修改即綠。
- 時間：2026-06-14 21:50

## 零新外部依賴、零新 env 變數；`llm_caller.py` 模組邊界不讀 config（新欄位預設值來自模組級 `DEFAULT_*` 常量，非 config 模組）。
- 時間：2026-06-14 21:50

## 【task #5 定稿】`RetryConfig` 統一退避入口最終簽章與相容策略（以程式碼實況為準，校正早期討論草案）
- 時間：2026-06-14 22:30
- 最終簽章（`studio/llm_caller.py`）：`RetryConfig(max_retries: int, base=DEFAULT_BACKOFF_BASE=2.0, cap=DEFAULT_BACKOFF_CAP=60.0, jitter=DEFAULT_BACKOFF_JITTER=0.0, backoff: Callable|None=None, sleep=_default_sleep)`；`as_kwargs()` 維持 export `{max_retries, backoff, sleep}` 不變。
- 相容策略：① 不傳 base/cap/jitter ⇒ 採 DEFAULT_*（jitter=0 確定值），自動生成退避等價舊 `backoff_delay` 預設，既有測試零改斷言即綠；② 顯式 `backoff` 注入優先（`__post_init__` 末尾 `if self.backoff is None` 才生成）；③ 非法值先 `warnings.warn(stacklevel=2)` 再 clamp，不拋例外不除零。
- 消費端定稿校正：早期決策記「`make_retry_config` 不遷移」，**實況已收斂**為「base/cap/jitter 欄位（建構快照）＋ `backoff=_backoff_delay`（retry lazy-read）同源同一組 `EXPERT_RATE_LIMIT_*` 鍵」雙路並存（experts.py:122-128）——欄位值與實際退避行為常態一致，且保留 lazy-read 語意（QA `test_negative_control_distinguishes_lazy_from_snapshot` 鎖死禁建構快照）。文件定稿一律以程式碼實況為準。
- 否決方案：`frozen=True`（可讀性損失 > 收益，改以 docstring 警語守 mutate 邊界，限制已登錄 KNOWN_LIMITATIONS）；jitter 預設改 0.25（破壞既有確定值測試斷言，向後相容優先，保留 0.0）。

## `_pending_awareness_context()` 只回傳資料（pending + in_progress 標題的 bullet 清單），不內嵌任何硬指令。
- 時間：2026-06-14 23:01
- 理由：函式若同時輸出資料與指令，重用時指令會帶著走，語意邊界模糊；分離後可單獨測試「清單內容」而非「整段提示文字」。
- 否決方案：把「不得提出與現有 pending 實質重疊」等硬指令寫進此函式——Senior 已標為設計異味，雖不阻擋本輪，本次直接避開比留技術債好。

## 兩條硬指令（「不得提出與上列任何項目實質重疊者」、「每點須來自不同子系統，優先覆蓋近期未碰過的模組」）移至 `_evaluate_self` 的 prompt 組裝層，緊接 `_pending_awareness_context()` 輸出之後，明確為上層決策。
- 時間：2026-06-14 23:01

## `_pending_awareness_context()` 與 `_filter_pending_duplicates()` 的清單來源統一為 `list_tasks("pending") + list_tasks("in_progress")`，兩者對齊，prompt 禁止清單與 pre-filter 擋截範圍完全一致。
- 時間：2026-06-14 23:01
- 理由：prompt 注入了 in_progress 任務要求不重疊，若 pre-filter 不擋 in_progress，「措辭滑溜的 in_progress 重複」仍可漏網——兩層防線的覆蓋範圍必須對齊。
- 否決方案：pending_titles 只含 pending——Engineer 與 Senior 均已指出此遺漏。

## `AUTOPILOT_DEDUP_RATIO` 以 `float(os.getenv("AUTOPILOT_DEDUP_RATIO", "0.75"))` 初始化為模組頂層常數，支援免改碼的運行期調整。
- 時間：2026-06-14 23:01
- 理由：0.75 是初始估值，中文字元級比對在同義改寫場景（「修復」vs「修正」）效果有限，上線後大概率需要調整；env override 讓調閾值的成本降為零，不需發版。
- 否決方案：硬編碼純常數——Engineer 明確指出這閾值之後大概率要調，不留出口是自縛手腳。
- **撤回（2026-06-14，critic 退回第 2 輪）**：與驗收標準 #4「零新 env 變數」直接衝突，此架構決策未對賬書面契約即拍板，屬假共識。實作已收斂為純模組常數 `AUTOPILOT_DEDUP_RATIO = 0.75`（移除 `TI_AUTOPILOT_DEDUP_RATIO`）。日後若確需 env override，須先由 PM 正式修訂驗收標準 #4 再加回。

## `_filter_pending_duplicates(proposals, existing_titles)` 比對前對雙方執行 normalize（`.strip().lower()`，去除首尾標點），以緩解長短句 ratio 被長度差稀釋的問題；normalize 邏輯獨立為內部 helper，方便日後替換。
- 時間：2026-06-14 23:01
- 理由：proposals 是完整句子，existing_titles 是標題，不 normalize 直接比對時 SequenceMatcher.ratio() 會被長度差稀釋，0.75 實際攔截率低於預期。

## PR 必須附 3–5 組中文邊界測試案例，記錄實測 ratio 值，涵蓋：同義詞替換（「修復」/「修正」）、縮寫（「單測」/「單元測試」）、英中混排（「retry/重試」）、完全不同主題（應為低 ratio 黑樣本）；測試案例本身成為閾值選取的溯源依據。
- 時間：2026-06-14 23:01
- 理由：0.75 目前是憑感覺拍的數字，無邊界案例則日後漏報時無從溯源；Senior 已明確列為核可條件。

## `_filter_pending_duplicates` docstring 加一行複雜度說明：`# O(n×m)，其中 n=proposals 數、m=existing 數；existing 預期 < 50 筆，若規模增長需重估`。
- 時間：2026-06-14 23:01

## 軟上限（topic cap）本輪不實作，決議記入 DECISIONS：理由為 topic bucket 分類需額外語意邏輯、誤殺風險尚未量化、prompt 廣度指示 + pre-filter 已覆蓋主要場景；待未來有隧道數據再評估。
- 時間：2026-06-14 23:01
- 否決方案：本輪同時做 topic cap——過度設計，且 bucket 分類若用關鍵詞切割容易誤殺跨模組任務。

## 測試四組（在原三組基礎上加一組）：(a) `_pending_awareness_context()` 輸出含每筆 pending + in_progress 標題；(b) prompt 組裝字串含兩條硬指令關鍵字；(c) `_filter_pending_duplicates()` feed 高重疊清單回傳為空，feed 不重疊清單全數保留；(d) 既有 autopilot/backlog 測試零回歸（不改動既有測試檔）。
- 時間：2026-06-14 23:01
- 理由：原設計把 (b) 與 (a) 合在同一個斷言——指令移到組裝層後需獨立斷言；(c) 需同時驗黑樣本（不重疊不誤殺）。

## `_is_duplicate`（`backlog.py`）及 `add_many` 契約完全不動；pre-filter 僅作用於本次 LLM 提案進場前，不回溯清洗現有 backlog，不刪除/合併任何既有任務。
- 時間：2026-06-14 23:01

## `REQUIRE_CHOWN` 為 `config.py` 模組頂層常數，`import` 時一次解析完畢；`require_chown_mode()` 只回傳 `REQUIRE_CHOWN`，不重讀 env
- 時間：2026-06-15 01:44
- 理由：`importlib.reload(config)` 即可切值，不需 cache invalidation；`require_chown_mode` 被 monkeypatch 成 lambda 時語意清晰，無副作用
- 否決方案：每次呼叫動態讀 env（`_reload_with` 斷言語意混亂，且 reload 後 REQUIRE_CHOWN 與 require_chown_mode() 可能不一致）

## `_parse_require_chown()` 內部直接複用 `env_bool()` 判 strict/off，`warn` 單獨處理，不另維護一份同義詞表
- 時間：2026-06-15 01:44
- 理由：工程師提醒——兩份同義詞表（1/true/yes/on）若不同步必然漂移，single source of truth 更安全
- 否決方案：`_parse_require_chown` 自己寫全套 if/elif（需維護兩份清單）

## 降級（warn/off）在 `_parse_require_chown` 解析時記 `logger.warning("TI_REQUIRE_CHOWN 降級至 %s ...", mode)`；unknown 值記 `"無法辨識"`；預設 strict 不記 warning
- 時間：2026-06-15 01:44
- 理由：escape_hatch 測試 `test_default_strict_no_warning` 明確要求預設路徑靜默；降級訊息在 import time 觸發確保 systemd journal 可見
- 否決方案：在 `require_chown_mode()` 呼叫時記（每次寫入都觸發，日誌爆炸）

## `env_bool()` 為 public 函式，空字串與 `None` 皆回傳 `default`
- 時間：2026-06-15 01:44
- 理由：測試直接斷言 `config.env_bool("TI_X_BOOL", True)`；對齊既有 `_env_float` 容錯慣例（空字串 = 未設定）
- 否決方案：private `_env_bool`（測試無法直接斷言）

## tmp 命名格式 `path.parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}"`
- 時間：2026-06-15 01:44
- 理由：高工指出同進程多 thread/coroutine 同時對同一 path 呼叫時，純 pid 會碰撞；`os.urandom` 已在 `import os` 範圍內，不引入新依賴；`O_EXCL` 確保碰撞時 open 失敗而非靜默覆蓋
- 否決方案：純 `os.getpid()`（同進程並發會碰撞）；threading.Lock 計數器（引入 threading 依賴，過度設計）；tempfile（前綴不可控，破壞 glob 清理斷言）

## fd 生命週期——happy path 在 `os.rename` 前 `os.close(fd)`；except 內 close 與 unlink 各自獨立 `try/except OSError: pass` 包覆
- 時間：2026-06-15 01:44
- 理由：高工指出 `os.close(fd)` 本身可能拋例外；若 close 失敗未獨立保護，後面的 `os.unlink(tmp)` 不會執行，tmp 殘留；兩步驟各自吞 OSError 才能保證兩件事都盡力執行
- 否決方案：單一 try 包住 close+unlink（close 失敗即跳出，unlink 不執行）

## 三態流程固定順序：`off` → 跳過 fchown/fstat 直接 rename；`warn` → fchown 失敗記 warning 後 rename 放行（不做 fstat）；`strict` → fchown 失敗 cleanup+raise → fstat uid≠0 cleanup+raise（訊息含實際 uid）→ fstat nlink≠1 cleanup+raise（訊息含 "nlink"）→ rename
- 時間：2026-06-15 01:44
- 理由：warn 的語意是「已知可能非 root，顯式接受」，fchown 後再做 fstat 驗證反而語意矛盾
- 否決方案：warn 也做 fstat（測試未要求，且「已知 chown 失敗下 fstat 仍驗」語意曖昧）

## `require_chown` 參數非 `None` 時記 `logger.warning("secure_write_root: require_chown 被呼叫端強制覆蓋為 %s，路徑 %s", mode_str, path)`
- 時間：2026-06-15 01:44
- 理由：高工指出此參數是安全邊界的旁路，生產環境若被誤用完全無 trace；警告不阻擋行為但留下稽核軌跡
- 否決方案：靜默覆蓋（security event 無 trace）；強制禁止此參數（測試需要直接傳入以 bypass config）

## `start_session` 建立 `events.jsonl` 走 `secure_write_root(events_path, b"")`；`record_event` 開頭加 `if not path.exists(): raise RuntimeError("events.jsonl 尚未初始化，請先呼叫 start_session")`
- 時間：2026-06-15 01:44
- 理由：高工指出若 `record_event` 先於 `start_session` 被呼叫，`.open("a")` 會靜默建出非 root-owned 檔案，破壞整個 strict 不變量；guard 讓問題在測試期間早死，不留隱性安全破口
- 否決方案：僅靠呼叫順序慣例（沒有任何防線，review 或測試順序錯誤即破功）；`assert path.exists()`（production 不跑 assert）

## `record_event` 的 append 維持 `.open("a")`，不呼叫 `secure_write_root`
- 時間：2026-06-15 01:44
- 理由：append 不改 owner；`secure_write_root` 是覆寫語意（tmp+rename），用於 append 會把整個 jsonl 清空成新內容
- 否決方案：每次 append 都走 secure_write_root（破壞 jsonl 多行語意）

## `backlog._save()` 在 `_locked()` 範圍內刪除舊 `tmp.write_text + tmp.replace`，改為 `secure_write_root(_path(state_dir), json.dumps(data, ...).encode("utf-8"))`；`_is_duplicate / add_many` 契約不動
- 時間：2026-06-15 01:44
- 理由：flock 序列化層保護整個 read-modify-write 區塊；secure_write_root 的內部 tmp+rename 在 lock 保護下執行，無 TOCTOU

## `config.py` 新增四個定義（`env_bool`、`REQUIRE_CHOWN_MODES`、`REQUIRE_CHOWN`、`require_chown_mode`）放在 autopilot 相關常數區段尾部，不動既有 `reload()` 邏輯
- 時間：2026-06-15 01:44
- 理由：頂層常數 reload 自動重解析；插入位置不影響任何現有 import 順序

## `README.md` 加一行 `| TI_REQUIRE_CHOWN | strict | root-only 寫入模式（安全預設） |` 至環境變數表，並在 Breaking Changes 區塊補 warn 過渡說明與 root 字樣；`.env.example` 加一行 `# TI_REQUIRE_CHOWN=strict  # Breaking change: 預設 strict，過渡期可設 warn`
- 時間：2026-06-15 01:44
- 理由：escape_hatch 測試五條文件斷言（含 `"Breaking change"`、`"warn"`、`"root"`、`"strict"`）均須通過；最小化改動不引入額外文件
- 否決方案：另開獨立 SECURITY.md（文件散落、測試斷言對象固定是 README）

## `NON_IDEMPOTENT_TOOLS = frozenset({"edit_file", "run_bash"})` 定義於 `tools.py` 頂層，加附註「由 providers 層去重邏輯讀取；新增工具時維護者須評估並更新此集合」
- 時間：2026-06-15 03:00
- 理由：tools.py 是工具規格的單一真實來源，分類應與規格同處；附註解決「誰負責更新」的認知問題
- 否決方案：放 providers.py 內 — 會讓 tools 與其分類定義分離兩處，工具新增時更容易遺漏

## 去重快取型別為 `dict[str, Any]`（非 `dict[str, str]`），初始化在 `__init__` 內（`self._dedup_cache: dict[str, Any] = {}`），並在 `speak()` 入口（snapshot 同層，約第 77 行）以 `self._dedup_cache = {}` 清空
- 時間：2026-06-15 03:00
- 理由：tools.execute 回傳值實際為 str，但型別標注從嚴；清空點在 speak 入口是唯一正確位置——放進 _attempt 內每次 retry 都清空等於功能報廢
- 否決方案：僅在 speak() 入口初始化、不在 __init__ — 有人新增旁路呼叫路徑時會踩 AttributeError

## key 推導公式 `f"{tool_name}:{hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:16]}"`，其中 `args` 明確指**已解析的 dict**（providers.py 第 93 行 parse 後的物件），不碰 `tc.function.arguments` 原字串
- 時間：2026-06-15 03:00
- 理由：`tc.function.arguments` 是 JSON 字串，序列化順序不穩定；`sort_keys=True` 必須作用在 dict 才有效，作用在字串則去重直接失效
- 否決方案：對原字串直接 hash — sort_keys 無效，同 args 不同序列化順序產生假 miss，功能靜默壞掉

## `[:16]` 截短加行內註解說明「per-speak tool call 數量極小（< 100），16 hex chars = 64 bits 碰撞機率 < 5×10⁻¹⁸，足夠」
- 時間：2026-06-15 03:00
- 理由：無解釋的魔法數字半年後會被懷疑是 bug，一行註解消滅維護成本

## 快取寫入點在 `await tools.execute(...)` 成功返回後；失敗（拋例外或回傳錯誤字串）不寫入快取
- 時間：2026-06-15 03:00
- 理由：防假命中——失敗結果若被快取，下次重放跳過副作用但回傳舊錯誤，問題靜默遮蔽

## providers.py 第 68–70 行 docstring 更新為「非冪等工具重放防護由 providers 層 per-speak `_dedup_cache` 處理，非 tools.execute 層」
- 時間：2026-06-15 03:00
- 理由：舊註解「須於 tools.execute 層自行防護」與本次實作相反，保留會誤導後人

## Claude provider 路徑的 retry gap 以 `核心改動: Claude provider 路徑缺乏 per-speak 去重保護，retry 時寫入型工具仍可重跑` 記入 backlog，本次不動 Claude path
- 時間：2026-06-15 03:00
- 理由：口頭「已知限制」無追蹤機制，六個月後會消失；進 backlog 才有人負責
- 否決方案：本次一併補 Claude path — 超出任務範圍，且 Claude path retry 機制需獨立確認，不能假設結構對稱

## `run_bash` 一律歸 non-idempotent，不解析命令內容判斷冪等性
- 時間：2026-06-15 03:00
- 否決方案：靜態分析 bash 命令 — LLM 可輕易繞過任何規則，且 false negative（漏防）比 false positive 風險高出數個量級

## `write_file` 歸冪等、不入去重；已知殘留風險（LLM 重放改內容）列為 #6 黑樣本測試化，不阻擋動工
- 時間：2026-06-15 03:00
- 理由：覆寫語意下同 args 重跑結果相同；納管後 args hash 不同時去重也攔不住，多一道邏輯卻無實際保護

## 整輪工具迴圈打包為單一 `_attempt` 函式，由 `run_with_retries` 統一控制退避邊界（providers.py:86–124）
- 時間：2026-06-15 04:15
- 理由：退避邏輯集中一處，接手人立即定位；分散在迴圈內的多點重試難以審計控制流。
- 否決方案：在 `_chat` 呼叫點逐次包 try/retry——導致工具執行與 LLM 呼叫的重試層次交錯，副作用計數不可控。

## `on_api_error` / `on_rate_limit_exhausted` 兩接點均回 `""`，不 re-raise、不含核可關鍵詞（providers.py:137–148）
- 時間：2026-06-15 04:15
- 理由：下游核可判定靠關鍵詞，空字串是唯一不污染判定的安全回退值。
- 否決方案：回傳 partial 累積文字——partial 可能含核可關鍵詞，下游誤判風險不可接受。

## `partial` 參數在兩個回退 callback 內刻意丟棄，並加行內註解 `# partial 刻意丟棄：回 "" 是設計，見設計決策` 防後人誤補
- 時間：2026-06-15 04:15
- 理由：無說明的丟棄會被「好心」填回去，一行註解消滅維護成本（高工審查意見）。
- 否決方案：靜默丟棄不加說明——已被高工指出為潛在維護地雷，拒絕。

## `idle` 廣播置於 `finally`，覆蓋成功／限流耗盡／api_error／未知例外四路徑（providers.py:166–167）
- 時間：2026-06-15 04:15
- 理由：finally 是唯一能保證四路徑不漏廣播的位置；分支內各自 broadcast 在加入新分支時容易漏。
- 否決方案：各 return 點前各自 broadcast——路徑增加時必漏，靠 code review 攔不住。

## per-speak `_dedup_cache` 在 `speak()` 入口重建，`new_attempt()` 只重置序號、保留結果快取（providers.py:84/90）
- 時間：2026-06-15 04:15
- 理由：序號重置確保 retry 重放時同位置呼叫命中同一 key；結果快取保留確保副作用只執行一次。
- 否決方案：每次 `_attempt` 清空整個快取——retry 重放時無快取可命中，非冪等工具副作用重跑，保護報廢。

## 重試重放時 `execute_deduped` 跳過副作用後，`broadcast(tool_use(...))` 仍觸發，列為下迭代 UX 待辦，本場不阻擋
- 時間：2026-06-15 04:15
- 理由：正確性無影響；過早修 UX 會讓本場範圍失控，且改動點（迴圈 L106–110）需獨立測試覆蓋（高工審查意見）。

## `metrics.outcome not in ("success", "")` 中 `""` 代表「回退路徑（耗盡／api_error）已被 callback 吸收、骨幹以空字串結案」，加行內註解說明（providers.py:163）
- 時間：2026-06-15 04:15
- 理由：無說明的 `""` 看起來像防禦性寫法卻無法自解，半年後會被懷疑是 bug（高工審查意見）。

## `complete_once` 不加第二層 `run_with_retries`，限流退避職責完全歸 `speak()` 層
- 時間：2026-06-15 04:15
- 理由：雙層退避疊加成語意不清的指數退避；兩端（Claude / OpenAI）已各自在 speak 層收斂，冒泡到本層的限流例外理論上不存在。
- 否決方案：complete_once 自套退避——與 speak 內退避疊加，退避次數與延遲語意混濁，難以調參。

## Claude provider 路徑的 per-speak 去重缺口不在本場補，已進核心 backlog（DECISIONS.md:573 / adr.json:998）
- 時間：2026-06-15 04:15
- 理由：口頭「已知限制」六個月後消失；進 backlog 才有人負責，本場範圍不擴張。
- 否決方案：順手補 Claude path 對稱保護——需改 experts.py 且無對應測試，屬本場範圍外的鍍金，拒絕。

## `studio/__init__.py` 在 `__version__` 行之後插入 `from . import secure_write`，全檔其他內容不動。
- 時間：2026-06-15 04:47
- 理由：讓 `from studio import secure_write` 的語意由「隱性 submodule lookup」變為「顯式套件匯出」，消除測試執行順序對 collection 的影響，且是業界標準做法。
- 否決方案：補 `__all__`——會把現有未宣告的 submodule 全部屏蔽，範圍遠超本場需求；否決在 `secure_write.py` 改動 import 鏈——問題根源在 `__init__.py`，不應改 production logic。

## 動工前以 `python3 -c "import studio"` 確認 import chain（`studio` → `secure_write` → `config`）無 side effect 或 circular dep，通過才合入。
- 時間：2026-06-15 04:47
- 理由：`__init__.py` 加入 import 後，`studio` 套件任何人 import 都會觸發 `secure_write` 的 import；若 `config` 有 import-time side effect，才會在此炸——用一條命令最便宜地確認。
- 否決方案：不驗直接合入——代價是若有問題，所有用 `import studio` 的測試與工具全炸，修復成本高。

## `studio/secure_write.py` line 79 的 `SecureWriteError` 訊息改為 `f"fchown(0,0) 失敗（非 root？）：{target}：{e}"`，其餘字串不動。
- 時間：2026-06-15 04:47
- 理由：`target`（`str`，來自 line 55 `os.fspath(path)`）在 except 塊可見；此格式同時滿足驗收測試三條斷言：`str(target) in msg`、`"chown" in msg.lower()`、`msg.strip() != ""`。
- 否決方案：改測試斷言放寬——「error message 含路徑」是可行動錯誤訊息的基本品質，應由 production code 承諾，弱化斷言即弱化契約；否決只放 `os.path.basename(target)`——`str(target)` 是全路徑，basename 不能通過斷言。

## 撤銷原任務 #2（修正 `test_qa_task3_failclosed_contract.py` 的 monkeypatch 對象）。`monkeypatch.setattr(os, "fchown", ...)` 與 `monkeypatch.setattr(secure_write.os, "fchown", ...)` 作用在同一 singleton 物件上，無 bug，兩者等效。
- 時間：2026-06-15 04:47
- 理由：`secure_write.py` 的 `import os` 與測試的 `import os` 均拿到 `sys.modules['os']`；patch 其屬性 `fchown` 即全域生效。研究員此點判斷有誤，不應引入不必要的改動。
- 否決方案：改為 `monkeypatch.setattr(secure_write, "os", ...)` 替換整個 os 物件——等效但侵入性更高；此方式值得寫進跟進待辦（防日後有人把 `import os` 改成 `from os import fchown`），本場不動。

## `test_studio_exports.py` 防呆測試列為核心 backlog 跟進待辦，本場不新增。
- 時間：2026-06-15 04:47
- 理由：本場驗收邊界是「7 模組 collection 通過且測試全綠」，防呆 guard 屬獨立改善項，硬塞只會讓範圍失控、驗收條件混淆。
- 否決方案：本場一起補——不阻擋進度卻增加 scope；先驗 7 模組全綠、再獨立 PR 補 guard，風險更低。

## 任務 #1（`__init__.py`）與 任務 #2-new（`secure_write.py` 錯誤訊息）並行起手，任務 #3（全量驗收 `pytest tests/autopilot/ -q`）待兩者合入後統一執行，驗收口徑為「7 模組全收集 + 全綠 + 無回歸」。
- 時間：2026-06-15 04:47

## `studio/providers.py:203` 的 `openai.AsyncOpenAI(...)` 加入 `max_retries=0`，行尾補單行註解 `# 讓位給 run_with_retries，避免 SDK 內建重試與外層退避雙層疊乘`。
- 時間：2026-06-15 05:28
- 理由：這是語意約束（SDK 永遠讓位），不是策略旋鈕；約束就在決策點旁，閱讀者不需跳外部文件。
- 否決方案：抽成 `config.SDK_MAX_RETRIES`——製造「可設非零」誤讀空間，且旋鈕沒有合理的非零使用情境。

## MiniMax 等 OpenAI 相容 provider 共用 `_openai_chat` → `_openai_client_args` 同一路徑，`max_retries=0` 一次修到位，不個別處理。
- 時間：2026-06-15 05:28

## 測試接縫為 `monkeypatch.setattr("openai.AsyncOpenAI", mock_cls)`（patch 頂層模組，非 `studio.providers.openai.AsyncOpenAI`），原因是 `_openai_chat` 用 lazy `import openai`，patch 本地屬性攔不到；PR 描述須明確說明此選擇。
- 時間：2026-06-15 05:28
- 理由：高級工程師指出 lazy import 的陷阱——patch 目標錯，測試永遠綠但鑑別力為零，連反向樣本的優點也被抵消。
- 否決方案：`monkeypatch.setattr("studio.providers.openai.AsyncOpenAI", ...)`——因 lazy import 不在 module 載入時綁定屬性，patch 無效。

## 正向斷言用 `mock_cls.call_args.kwargs["max_retries"] == 0`（驗收傳入的建構參數），不實際建構真實 client。
- 時間：2026-06-15 05:28

## 反向樣本（證明測試有鑑別力）用 `openai.AsyncOpenAI(api_key="sk-test").max_retries == 2`，必須帶 `api_key` 避免 `OpenAIError`；「2」為 SDK 外部預設值，未來若 SDK 改預設會無辜變紅，屬可接受的已知風險，在測試註解說明即可。
- 時間：2026-06-15 05:28

## `experts.py::_build_client()` 的 docstring 補兩行明確說明：`# 重試由 speak() 層的 run_with_retries 統一管控；ClaudeSDKClient 本身不做額外退避，避免雙層疊乘。`
- 時間：2026-06-15 05:28
- 理由：「一行補充」措辭模糊，未來維護者可能「好心」加上重試；明確點名 ClaudeSDKClient 無額外退避，才能防止誤改。
- 否決方案：寫進 KNOWN_LIMITATIONS 或新增測試——前者需跳外部文件、後者屬越界驗收（Claude 重試行為應由 Ti SDK 側守）。

## `_openai_chat` 每次建構新 `AsyncOpenAI` client（無連線池重用）列為**範圍外技術債**，進下個 sprint backlog，本次不阻擋。
- 時間：2026-06-15 05:28

## 全量驗收指令 `python3 -m pytest tests/autopilot/ tests/core/ -q`，基線 1058 passed；任何回歸（含 push_merge_flags、merge_outcomes、_wait_for_ci）皆視為阻斷，不允許「範圍外測試變紅」被忽略。
- 時間：2026-06-15 05:28

## _SUBSYSTEM_KEYWORDS 所有 pattern 一律加 \b word boundary，匹配時套 re.IGNORECASE
- 時間：2026-06-15 06:08
- 理由：`re.search(r'ci', "social")` 等會 false positive 打到無關英文詞，污染子系統計數器，導致合法提案被錯誤攔截——這是正確性 bug，非偏好問題。
- 否決方案：不加 `\b` 直接用原始短字串（會打到 decide、emergence、social 等，不可接受）。

## 中文 keyword（去重、評估）改用 negative lookahead/lookbehind 保護，不依賴 \b 的 Unicode 語義
- 時間：2026-06-15 06:08
- 理由：Python `re` 的 `\b` 對 Unicode CJK 字元與 ASCII 混排時邊界語義不穩定；`(?<![^\s，。！？])去重(?![^\s，。！？])` 或等效寫法比賭 `\b` 可靠。
- 否決方案：直接用 `\b去重\b`（實測前不可信賴，已有前例踩坑）。

## _SUBSYSTEM_KEYWORDS 初始清單改為帶邊界的 pattern 列表，如下
- 時間：2026-06-15 06:08

## _extract_subsystems 匹配時固定傳入 re.IGNORECASE，不由呼叫端決定
- 時間：2026-06-15 06:08

## _count_subsystem_coverage 回傳型別明確為 collections.Counter[str]，docstring 標明
- 時間：2026-06-15 06:08
- 理由：`Counter` 直接支援 `.most_common()`、`>= K` 比較，且型別明確；呼叫端不猜回傳結構。
- 否決方案：回傳 `dict[str, int]`（可行但語意不如 Counter 清晰，且日後呼叫端可能重造輪子）。

## 0.55 閾值的主力論述修正——PR 描述須明確說明「0.55 是邊緣補強，同子系統 K-filter 才是治隧道的主防線」
- 時間：2026-06-15 06:08
- 理由：工程師實測指出真實長標題 0.75 已能抓到，0.55 僅對極短同義標題有效；若 PR 把 0.55 包裝成主修，維護者日後調 threshold 時會誤判影響範圍。

## 在 known-limitation 測試裡加一條「0.55 錯誤攔截」的反向哨兵，標記為 xfail 或明確 assert 被擋，CI 永遠看得到代價
- 時間：2026-06-15 06:08
- 否決方案：只測「該擋的」而不測「不該擋的」——鑑別力為零，等同假綠。

## collection guard 的 TYPE_CHECKING 往上 10 行啟發式，加一個「guard 在第 12 行」反向樣本確認邊界行為，並在測試 docstring 明確標記「10 行是啟發值，非精確語義邊界」
- 時間：2026-06-15 06:08
- 理由：高級工程師指出若 guard 在 20 行外會漏攔；此反向樣本讓邊界可見、CI 可驗，而非文件死角。

## 其餘既有決策維持不變——閾值單一常數 AUTOPILOT_DEDUP_RATIO、不引入 jieba、K 放 config、不回溯 backlog、模組邊界全在 autopilot.py
- 時間：2026-06-15 06:08

## 新節插入錨點固定為「`## 設定流程` 節結束後、`## 指定 GitHub repo` 之前」（L286 之後），不以區間描述
- 時間：2026-06-15 06:47
- 理由：工程師核查出 L296 存在第三節，原「之後…之前」描述有兩個落點；單一錨點讓執行零歧義
- 否決方案：描述為「設定流程後、雙軌路由前」——中間夾了 `指定 GitHub repo`，區間含糊

## 流向圖改用單行內文 `` make_retry_config() → RetryConfig → run_with_retries ``，不使用 ASCII art
- 時間：2026-06-15 06:47
- 理由：ASCII art 在 merge conflict 時對齊易跑掉且無工具保護；單行可讀性等價，維護成本為零
- 否決方案：ASCII 三層流向圖——視覺化代價高於收益，且三層架構本身已夠簡單

## 流向圖只畫「工廠 → config 物件 → 單一執行器」一條路徑，Claude / OpenAI 收斂點在 `run_with_retries`，不畫兩條分叉
- 時間：2026-06-15 06:47
- 理由：畫兩條獨立路徑與「禁第二層 retry」禁令自相矛盾；單路徑才能正確傳達「provider 層無自帶 retry」的語意

## jitter 語意只寫「jitter 為比例值（非秒數）」，不硬寫 `[0,1]` 具體範圍，改加「語意見 `llm_caller.py` 實作備註」指向源頭
- 時間：2026-06-15 06:47
- 理由：若實作改為 `[0, 0.5]` 文件即過時；指向源頭讓文件穩定，實作是唯一真相
- 否決方案：文件直寫 `[0,1]`——取捨表聲稱「公式改動不需同步文件」但同時寫死範圍，自相矛盾

## 「禁第二層 retry」禁令後緊接一行偵測提示：「症狀為 log 出現指數級累積等待（單次 retry 延遲超過 `EXPERT_RATE_LIMIT_BACKOFF_CAP` 數倍），可 grep `retry attempt` 行的 delay 欄位確認」
- 時間：2026-06-15 06:47
- 理由：純文件禁令無執行力；加偵測提示讓維護者有具體查核點，半年後禁令仍有效
- 否決方案：僅寫禁令文字——高級工程師明確指出純文字禁令通常第一個被新人繞過

## 節內三段結構維持「角色與資料流 → 新 provider 接入契約 → 語意備忘」順序不變
- 時間：2026-06-15 06:47

## 新節標題與層級維持 `## LLM 韌性中介層（retry 子系統）`，H2 齊平其他架構節
- 時間：2026-06-15 06:47

## 模組職責表僅在 `experts.py`、`llm_caller.py`、`providers.py` 三列末補一句指回新節，不新增欄位、不重排
- 時間：2026-06-15 06:47

## `make_retry_config()` 工廠本次不搬移，以「架構伏筆：」前綴標注接入點預留，明確標示「非現行需求」
- 時間：2026-06-15 06:47

## Tenacity / backoff 套件評選不入文，僅加一句「設計與 exponential-backoff-with-jitter 慣例對齊」交代來源，不展開比較
- 時間：2026-06-15 06:47

## 反向黑樣本測試使用 in-memory 字串操作——讀 CHANGELOG.md 內容後用 Python str 截掉 Breaking 區塊，對截斷後的字串跑斷言；不動真實檔案、不依賴 tmp_path 寫回磁碟
- 時間：2026-06-15 07:18
- 理由：in-memory 最輕量，不產生任何磁碟副作用；測試 crash 也不會殘留；同時避免工程師用 tmp_path 時因路徑指向錯誤而假綠
- 否決方案：改動真實 CHANGELOG.md 後還原（crash 就殘留）；寫入 tmp_path 再讀（多一層 I/O，失敗模式更多）

## 版本讀取方式指定 tomllib.load()（Python 3.11+ 標準庫，3.12 環境內建，無額外依賴）；pyproject.toml 路徑用 Path(__file__).parents[N] 推 repo root，不用 cwd；N 值在實作前先用 Explore 確認測試檔深度
- 時間：2026-06-15 07:18
- 理由：importlib.metadata.version() 需套件已安裝，CI 裸 checkout 沒裝就爆；tomllib 只需檔案存在即可，與安裝狀態無關
- 否決方案：importlib.metadata（CI 安裝前提不受控）；regex parse（脆，版本格式一改即壞）

## README 錨點互指納入測試範疇，但只斷言「README 檔案內含 state 安全寫入 這個字串」，不追 HTML anchor hash；CHANGELOG 內的指向文字必須與此字串一致
- 時間：2026-06-15 07:18
- 理由：完整 anchor 是 GitHub Markdown 自動生成，測試無法直接驗；測 raw text 已能防止小節被悄悄改名後連結語意斷開；且 test_qa_task5 已有 README 字樣掃描，新測試只需互指一致性即可，不重複
- 否決方案：完全不測（靜默死鏈，高工明確指出不可接受）；測 HTML anchor（#state-安全寫入 這類 hash，跨平台渲染不一致）

## 動工第一步執行 grep -rn "0.1.0" tests/，若既有測試硬寫版本字串則先修正再升版；此步驟必須在 pyproject.toml 改版之前完成
- 時間：2026-06-15 07:18
- 理由：工程師已提醒，既有測試寫死舊版本字串會造成升版後連帶紅燈，需先排雷

## 版本字串升 0.2.0，pyproject.toml 為單一事實來源；CHANGELOG.md 與測試皆從 pyproject.toml 讀取，不在兩處硬寫版本字串
- 時間：2026-06-15 07:18

## CHANGELOG.md 落點 repo 根目錄，Keep a Changelog 格式，## ⚠️ Breaking Changes 作為 0.2.0 節最頂端子區塊，位置先於 ### Changed / ### Added
- 時間：2026-06-15 07:18

## Breaking 條目時序語意鎖定「宣告已發生」——用現在完成式（「已改為 strict 預設，自 0.2.0 起生效」），禁止出現「下版才 enforce」「警告期後」等未來時序
- 時間：2026-06-15 07:18
- 否決方案：研究員兩階段過渡模板（與 config.py 第 695 行 strict 已成立矛盾）

## warn 描述為「使用者側逃生艙」——「若非 root 環境，顯式設 TI_REQUIRE_CHOWN=warn 可延後遷移」，不承諾任何我們自己的緩衝期
- 時間：2026-06-15 07:18

## 錯值 fail-safe 行為（無法辨識值 → strict + warning）必須在 Breaking 條目內明示一行，防使用者誤以為打錯字會靜默降級
- 時間：2026-06-15 07:18

## 四要素順序固定（①行為變動 ②原因 ③before/after 遷移 ④生效版本），測試用 index 比相對位置而非整段字串比對，保留鑑別力且不過度僵化
- 時間：2026-06-15 07:18
- 理由：工程師已指出逐字比對會脆；index 比法維護成本低、改字不紅、但調換順序會紅

## 本次不引入 semantic-release / git tag 自動化；在 CHANGELOG 或 DECISIONS 加「架構伏筆：」標注留接入點，非現行需求
- 時間：2026-06-15 07:18

## [任務 #1 落地] release note 檔案路徑 = 根目錄 `CHANGELOG.md`（Keep a Changelog）、本次版本字串 = `0.2.0`，pyproject.toml 已升版
- 時間：2026-06-15 07:19
- 角色：工程師
- 落地內容：
  1. 已執行排雷 `grep -rn "0.1.0" tests/` → **無命中**，既有測試未硬寫舊版本字串，升版無連帶紅燈風險。
  2. `pyproject.toml` `version` 由 `0.1.0` → `0.2.0`，作為版本單一事實來源；後續 CHANGELOG.md 與測試一律從此處讀取（tomllib），不在他處硬寫版本字串。
  3. release note 落點確認為 repo 根目錄 `CHANGELOG.md`（先前無任何 CHANGELOG/RELEASE 檔，本次新建為 #2 任務範圍）。
- 範圍守線：本任務僅決策＋升版，**不碰 chown 程式邏輯**（`config.py` 已 strict 預設、warn/off 三態、錯值 fail-safe，均維持原狀）。
- 架構伏筆：semantic-release / git tag 自動擷取 `BREAKING CHANGE:` footer 留待未來；本次 CHANGELOG 為人工維護。

## `studio/__init__.py` F401 採 redundant-alias：`from . import secure_write as secure_write`，同行加 inline comment `# re-export; 'as' 是 ruff F401 慣用消法，請勿移除`
- 時間：2026-06-15 08:07
- 理由：comment 跟著 code 走，PR description 不會；防止新人善意刪掉成本最低
- 否決方案：加 `__all__` ——若原本無 `__all__`，`from studio import *` 語意改變，潛在 breaking，可逆性不足

## E731 兩處 lambda 手動改為 if/else 內 `def op()`，不使用 `--unsafe-fixes`
- 時間：2026-06-15 08:07
- 理由：手動改強迫確認語意等價並留審計痕跡；本案 lambda 無參無 closure，語意已確認等價
- 否決方案：`--unsafe-fixes` 自動修——無審計痕跡，萬一有 closure 邊界 case 靜默改壞

## I001（import 排序）與 tests/ 未用 import（F401）用 `ruff check --fix` 處理，但必須分檔指定，禁止對整個 `studio/ tests/` 全域跑 `--fix`
- 時間：2026-06-15 08:07
- 理由：全域 `--fix` 會把邊界外的 safe fix 一起改掉，scope creep 無法防控
- 否決方案：`ruff check --fix studio/ tests/` 一鍵跑——改動面不可控

## E731 所在檔（`tests/autopilot/test_qa_task4_dualpath_parity.py`）必須從 `--fix` 的掃描範圍中排除，確認不與手改並行衝突後，#1~#4 才可並行執行；若無法排除則 #2 串行先行
- 時間：2026-06-15 08:07
- 理由：`--fix` 批次與手改 lambda 撞同一檔會產生 conflict，並行假設須先驗證

## E741 變數重命名，以 `line` 作為首選錨點；若語意不符（非行內容）工程師可自裁，但須在 PR 說明改名原因
- 時間：2026-06-15 08:07
- 理由：給錨點降低 review 來回摩擦，不硬規定保留彈性
- 否決方案：完全「工程師自裁」不給錨點——多處 `l` 出現時容易命名不一致引發 review 往返

## 驗收前必須確認 `pyproject.toml` 的 `select` 包含 E、F、I 系列（即 E731、E741、I001 均在規則集內），再跑 `ruff check`；若規則未啟用，通過不代表有跑
- 時間：2026-06-15 08:07
- 理由：ruff 通過但規則沒啟用等於假綠，需先排除

## 驗收指令三步驟依序執行：① `ruff check studio/ tests/`（exit 0）→ ② `python3 -m pytest tests/ --collect-only -q`（≥472 筆無 error）→ ③ `git diff --stat` 人眼核對改動檔清單未越界
- 時間：2026-06-15 08:07
- 理由：collect-only 只證 import 不爆；`git diff --stat` 才能防 scope creep

## 改動邊界硬鎖「消 lint」——任何超出此邊界的改動（重構邏輯、清無關 code smell、補測試）一律拒絕進本 PR，PR description 須明文說明此限制
- 時間：2026-06-15 08:07
- 否決方案：順手清鄰近 code smell——lint 修正 PR 最常見的 scope creep 入口，一律擋掉

## 禁止用 `# noqa` 作為本次任何違規的解法；本次所有 8 個違規均有等價合規寫法
- 時間：2026-06-15 08:07
- 否決方案：部分加 `# noqa` 快速過關——把 lint 債藏起來傳給下一個人

## 三個任務合入**一個**新測試檔 `tests/core/test_tools_dedup_task6.py`，以 A/B/C section 分隔，不拆三個獨立檔
- 時間：2026-06-15 08:38
- 理由：task5 單檔三 section 已有先例；三個任務共用 `_run` / `_no_sandbox` 各寫一次而非三次，減少 ~2/3 樣板，維護成本下降；「可並行實作」指開發不互相阻塞，並非強制物理分檔
- 否決方案：三個獨立檔（工程師初提）——功能等價，但 fixture 三倍複製是確定的維護債，得不償失

## `_run` helper 在新檔中複製一次（`def _run(coro): asyncio.run(coro)`），不引入 conftest；於 PR description 明記「此為已知技術債，task3/task5/task6 各自持有一份，日後一次性遷移 conftest」
- 時間：2026-06-15 08:38
- 否決方案：立即移入 conftest（高工輕量建議）——改動邊界硬鎖「只加測試」，conftest 屬共用基礎設施，此 PR 修改會超出 scope

## `_no_sandbox` fixture 複製入新檔，docstring 說明「args drift 的斷言是副作用行數 == 2，要求 run_bash 真正落地，故必須關 sandbox」
- 時間：2026-06-15 08:38
- 理由：高工確認 args drift 選「行數 == 2」比「斷言 key 不同」意圖更直接，但這個選擇同時要求 sandbox 關閉才能讓 append 真正發生

## **#1 命門硬鎖**：兩次 attempt 之間必須呼叫 `cache.new_attempt()`；test docstring 標注「若省略此呼叫，`_seen[base]` 不重置，attempt2 取到 `#1` ≠ `#0` → cache miss → `execute` 再跑一次 → `call_count==2` → spy 斷言直接紅」
- 時間：2026-06-15 08:38
- 理由：工程師點出的命門，設計文字提到 new_attempt 但未說清楚後果；實作者若只照抄 task3 模式、少一行呼叫，兩個斷言會各自紅掉但原因不明顯，提前標注可省 debug 時間

## **#1 spy 作用域**：`with patch("studio.tools.execute", wraps=tools.execute) as mock_execute:` context manager **包住 attempt1 + attempt2 整個序列**，並在 context 外補斷言 `mock_execute.call_count == 1` AND 檔案行數 == 1
- 時間：2026-06-15 08:38

## **#2 patch.object 啟動時序**：`with patch.object(cache, "put", wraps=cache.put) as mock_put:` 必須在 `cache.new_attempt()` 呼叫**之前**啟動；若反序，`wraps=cache.put` 求值時方法已被替換，出現 mock 包 mock 行為
- 時間：2026-06-15 08:38
- 理由：高工明確點出此時序風險，put 是 bound method，patch.object 在啟動時捕捉原始 reference，時序錯誤靜默不報錯但 wraps 語意破壞

## **#2 涵蓋三個冪等工具**：`write_file` / `read_file` / `web_fetch` 各一條測試，每條斷言 `mock_put.call_count == 0`；`web_fetch` 另 `monkeypatch.setattr(tools, "_research_fetch", fake_fetch)` 免真連線（同 task5 模式）
- 時間：2026-06-15 08:38

## **#3 parametrize 三案**（確認定稿）：
- 時間：2026-06-15 08:38

## Case3 docstring 標注：「int vs str type drift 在 `json.dumps(sort_keys=True)` 層同理（digest 必異），但現有工具規格全 string、整數 command 會讓 runner 失敗，無法取得 2 個成功副作用，故以大小寫差異作 value content drift proxy；此測試驗的是『副作用多跑（at-least-once）』而非 hash 本身」
- 時間：2026-06-15 08:38
- 否決方案：直接傳整數 command 測 int/str type drift——執行必失敗，取不到兩個副作用計數，鑑別力反而更低

## 新檔 docstring 或 PR description 標注「`patch(wraps=...)` 自動偵測 coroutine function 為 `AsyncMock` 需 Python ≥ 3.8；若跑更早版本須手動指定 `new_callable=AsyncMock`」
- 時間：2026-06-15 08:38
- 否決方案：加 `new_callable=AsyncMock` 防禦性寫死——不必要，現行 CI 環境已 3.8+，加了反而讓讀者誤以為自動偵測不可信

## 驗收三步驟順序不可顛倒：① `ruff check studio/ tests/`（exit 0）→ ② `python3 -m pytest tests/core/ -q`（≥ 既有 15 + 新增數量，無 error）→ ③ `git diff --stat` 人眼核對只出現 `tests/core/test_tools_dedup_task6.py` 一個新檔，不得有生產碼異動
- 時間：2026-06-15 08:38

## 任務定性維持「驗收關閉、零 diff」——不產出任何生產碼異動。
- 時間：2026-06-15 09:00
- 理由：工程師實跑已證偽「HEAD 未修復」這個前提；ruff exit 0 + 2555 tests collected exit 0 + git diff 空，三項齊全。

## 防回歸唯一機制為 `from . import secure_write as secure_write` 的 redundant alias 語法觸發 ruff F401，且 ruff 必須在 CI pipeline 中執行；inline comment 不計入防護機制。
- 時間：2026-06-15 09:00
- 理由：工程師與高工一致指出 comment 不是硬約束、import 清理工具不讀註解；`X as X` 是 ruff 的豁免名單訊號而非 fix 對象，這才是機制。
- 否決方案：「collect 即驗」列為防線——collect 只跑 pytest，ruff 是另一條指令，兩者路徑不同；collect 綠不等於 ruff 綠，不可混稱「防護」。

## 驗收指令組由兩條擴充為三條，順序為：① `ruff check studio/ tests/`（exit 0）→ ② `python -c "from studio import secure_write; print(secure_write)"`（runtime import 無例外）→ ③ `python3 -m pytest --collect-only -q tests/`（exit 0, 0 collection error）。
- 時間：2026-06-15 09:00
- 理由：高工指出 collect 看不到 circular import 或 lazy load 導致的 runtime 炸點；第②條成本幾乎為零，覆蓋 collect 的盲點。
- 否決方案：只跑兩條指令——驗收範圍不完整，collect 過不等於 runtime import 正常。

## 關閉前必須執行 `git log --oneline studio/__init__.py` 確認有修復 commit 且日期早於研究員調研清單；若無此 commit 記錄，「真因為清單過期」假設不成立，需重新診斷。
- 時間：2026-06-15 09:00
- 理由：高工點出「HEAD 已修復」是推論而非工具輸出，需用 git log 實證才能閉環；若假設錯誤，整個零 diff 結論就崩。
- 否決方案：僅憑 ruff 綠就直接關閉——繞過了對假設前提的最小驗證，風險不對稱。

## 關閉說明文字中，防回歸機制段落只寫一句：「唯一防線 = `from . import secure_write as secure_write` 觸發 ruff F401，且 ruff 在 CI」，刪除 collect 即驗與 inline comment 的保護力描述。
- 時間：2026-06-15 09:00

## 同義詞正規化拆成兩道 pass，不用單一扁平 dict + str.replace。
- 時間：2026-06-15 09:19
- 理由：ASCII 字串替換無邊界保護，`add`/`fix` 會命中 `address`/`prefix`/`fixture`，是設計表裡就收的詞，必須修正。
- 否決方案：單一 `_SYNONYM_CANONICAL` + `s.replace(syn, f" {can} ")` 全串掃——會靜默汙染 ASCII 子字串，不可用。

## Pass 1（字串級，CJK 多字詞 → ASCII canonical）放在 `_normalize_for_dedup`，常數命名 `_SYNONYM_CJK_TO_CANONICAL`，替換前按 key 長度降冪排序（長詞優先）。
- 時間：2026-06-15 09:19
- 理由：CJK 詞（去重、修復等）在逐字切 token 前需提前展開為 ASCII，否則切後 token 是單字，Jaccard 無法對比；長詞優先避免 deduplication→dedup 被短者先截斷。

## Pass 2（token 級，ASCII → ASCII canonical）在 `_tokenize_for_dedup` 切完後，對每個 token 做 `dict.get(tok, tok)`，常數命名 `_SYNONYM_ASCII_TO_CANONICAL`（如 fix/add/improve 的同義 ASCII 詞）。
- 時間：2026-06-15 09:19
- 理由：token 已是完整切割片段，`dict.get` 精確匹配，零子字串風險，不需 padding 也不需邊界正則。
- 否決方案：token 層用 str.replace——在 token 已是完整字串時 str.replace 與 dict.get 等效，但 dict.get 語意更清晰。

## 不在 Pass 1 對 CJK 加邊界保護（正則 `\b` 對 CJK 無效，且同義詞限 ≥2 字已足夠隔離）。在 `_SYNONYM_CJK_TO_CANONICAL` 上方加 docstring 警告：「字串級無邊界保護；同義詞請選 ≥2 字且辨識度高的詞，避免同義前綴（如不得加單字 `改`）。」
- 時間：2026-06-15 09:19

## 測試檔落在 `tests/autopilot/test_dedup_synonym_task7.py`，不放 `tests/core/`。
- 時間：2026-06-15 09:19
- 理由：高工確認現有同義詞測試已在 `tests/autopilot/`，`tests/core/` 是 infra 層；混放違反目錄語意，掃描路徑也要一致。
- 否決方案：`tests/core/test_dedup_synonym_task7.py`——目錄錯位，不採。

## 測試 ③「字串等值契約」的 docstring 必須同時標注雙重行為：`backlog._is_duplicate("修正去重邏輯", "修復去重邏輯")` → False（維持等值合約）；同一對輸入進 `_filter_pending_duplicates` → 攔截（同義展開後 Jaccard ≈ 1.0）。兩個斷言寫在同一 test 函式內，防止讀者誤判「③ 回傳 False = 系統無防護」。
- 時間：2026-06-15 09:19
- 理由：高工指出這是兩個對立事實共存；只測其一會製造誤解。

## 測試補第五類（子字串汙染反例）：含 `add`/`fix` 子串的英文標題（`address prefix`、`fixture toolkit`），Pass 1+2 正規化後 token 不可含 `add` 或 `fix` canonical；此為黑樣本，用以驗證 ASCII 子字串汙染不再發生。
- 時間：2026-06-15 09:19
- 理由：工程師指出這條測試才能抓出 naive replace 的 bug；不加就驗不出設計修正有效。

## 異動檔案邊界最終確認：studio/autopilot.py（+兩常數 + _normalize_for_dedup Pass 1 + _tokenize_for_dedup Pass 2 共約 10 行）、tests/autopilot/test_dedup_synonym_task7.py（新增）。不動 config.py、backlog.py、任何既有測試檔案。
- 時間：2026-06-15 09:19

## 本任務定性為「驗證關閉」，交付形式為空 diff + 守護測試全綠（11 passed），不產生任何 .py / .md 變更。
- 時間：2026-06-15 10:19
- 理由：lint 與 ARCHITECTURE 兩半在當前 HEAD 皆已滿足，重做正確交付屬返工。

## 不新增 `retry_config` 別名；正式符號固定為 `make_retry_config()`，需求快照中的舊字樣不反映為 API 表面。
- 時間：2026-06-15 10:19
- 理由：YAGNI——API 表面保持最小，舊快照字樣不值得引入額外入口。
- 否決方案：新增 alias `retry_config = make_retry_config` ——製造兩個名字，未來清理成本高於零收益。

## 未來若需統一符號，做法為直接改名 `make_retry_config` + 同步改測試錨點，禁止先加 alias 再留著。
- 時間：2026-06-15 10:19
- 理由：不留別名正是此刻的可逆性投資；改名比消除別名便宜。
- 否決方案：alias 過渡期——在符號分歧上疊一層，只是把清理日期往後推。

## 禁止 provider 層自帶第二層 retry（OpenAI 端維持 max_retries=0）；新 provider 接入必須依 ARCHITECTURE.md L318–325 三步契約走。
- 時間：2026-06-15 10:19
- 理由：兩層退避疊加導致指數爆炸，且 log 偵測症狀已標注，後人查得到。
- 否決方案：讓各 provider 自管 retry——退避語意碎片化，難以統一調參。

## `make_retry_config()` 目前住在 `experts.py`（業務層）卻被 `providers.py`（基礎設施層）反向 import，此依賴方向反轉列為具名技術債，以 backlog 工單追蹤，不靠 ARCHITECTURE.md「伏筆」二字承載。
- 時間：2026-06-15 10:19
- 理由：文字伏筆沒有觸發機制，容易在後續迭代中消失；工單有狀態、有觸發條件才可追蹤。
- 否決方案：繼續只靠 ARCHITECTURE.md 備注——高工指出「伏筆」無觸發器，風險在第三個 provider 接入前被遺忘。

## 上述 backlog 工單的觸發條件為「第三個 provider 接入時」；屆時將 `make_retry_config()` 遷移至 `llm_caller.py`（provider 無關的 retry 骨幹），消除反向依賴。
- 時間：2026-06-15 10:19
- 理由：遷移時機與動因明確，不提前搬動避免無謂改動；`llm_caller.py` 本就是 provider 無關層，是正確的落點。

## 守護測試 `test_task1_retry_doc.py` 是 ARCHITECTURE.md 的活文件護欄，後續任何文件或符號改動必須同步維護測試，兩端不得獨立異動。
- 時間：2026-06-15 10:19
- 理由：行號錨若文件重排會脆（工程師已指出），但此屬維護紀律而非設計缺陷，不阻擋本次關閉。

## `test_as_kwargs_packs_three_keys` 的 `body[:600]` magic number 列為低優先技術債備忘，不阻擋本次關閉；後續可改為動態計算函式體長度上限。
- 時間：2026-06-15 10:19

## 空 diff 交付基準 = `cc46ccb`（E741+I001 修復）已合入 `main`，程式碼 `.py` diff 為空、本任務無源碼變更；`task-4` 分支唯一差異為本 ADR 文件自身，確認非假綠。
- 時間：2026-06-15 10:34
- 理由：工程師要求「能追 commit」；`cc46ccb` 在 `main` branch 可查，任何人執行 `git log main --oneline | grep cc46ccb` 可重現此結論。
- 否決方案：僅憑「diff 為空」聲稱關閉但無 commit 錨點——彼時假綠無從分辨。

## E741 處置 = 改名 `l → line`，禁止 `# noqa: E741`。
- 時間：2026-06-15 10:34
- 理由：改名零成本且語意更清晰；`noqa` 是壓警告符號，屬引入技術債，只保留給 stub 等無控制權場景。
- 否決方案：`# noqa: E741` 在有控制權的自有檔案上使用。

## Import 排序 = `ruff check --fix` 自動修，禁手動排序。
- 時間：2026-06-15 10:34
- 理由：手排易漏、製造無意義 review 噪音；工具排序是事實標準，已修結果可被 `ruff check .` 幂等驗證。
- 否決方案：手動調整 import 順序（人工判斷與 ruff 規則不一定等價）。

## CHANGELOG 語氣 = 即刻生效逃生艙式（`TI_REQUIRE_CHOWN=warn`），禁 deprecation 過渡警告語氣。
- 時間：2026-06-15 10:34
- 理由：`strict` 已是當下預設，非未來計劃；「過渡期將在 X.Y 移除」語氣屬誤導，讓用戶誤判仍有緩衝。
- 否決方案：「下個 major 版本移除」deprecation 語氣——與已成立事實不符。

## 四要素規範正本住在 `test_release_note_breaking.py` 的模組 docstring，不另立 CONTRIBUTING.md 副本。
- 時間：2026-06-15 10:34
- 理由：測試已含六點說明（行為變動／原因／before-after／生效版本／反向黑樣本／README 互指），定義與斷言共存，後人看測試就能理解規範；雙寫副本必然分歧。高工「憑什麼知道要寫哪四樣」的問題，答案是「看這個測試的 docstring」。
- 否決方案：在 CONTRIBUTING.md 另寫一份四要素說明（兩份真相來源，長期維護必漂移）。

## 測試斷言模式 = 讀真實 CHANGELOG.md 解析關鍵字順序，禁止在測試裡硬編 CHANGELOG 副本。
- 時間：2026-06-15 10:34
- 理由：測試已採 `CHANGELOG = ROOT / "CHANGELOG.md"` 讀檔，四要素以 `index` 比相對順序而非逐字比對；改字不紅、調換順序才紅——這正是工程師要求的「真護欄」，已就位。
- 否決方案：測試裡硬編一份預期字串副本（CHANGELOG 改動後測試永遠不紅，等同假護欄）。

## CHANGELOG 的 release pipeline 曝光（tag notes / email banner）列為跟進待辦，不阻擋本次交付。
- 時間：2026-06-15 10:34
- 理由：高工指出「逃生艙不曝光等同不存在」屬正當風險；但 release pipeline 配置超出本次任務邊界，強行納入會擴大範圍且引入 CI/CD 依賴。觸發條件：下次 `0.2.0` tag 打出前由 DevOps 確認 tag notes 包含 CHANGELOG 頂層 Breaking Changes 區塊。
- 否決方案：以「文件已寫」替代「pipeline 曝光確認」——靜默升級用戶不讀 CHANGELOG，逃生艙形同虛設。

## 「守護測試與 CHANGELOG 不得獨立異動」維護紀律，以 PR checklist 顯式條目承載，由 reviewer 強制勾選，跟進待辦補入 `CONTRIBUTING.md`。
- 時間：2026-06-15 10:34
- 理由：工程師已點名此為人治約束，遲早會漏；但技術上測試讀檔已消除最大漏洞（hardcoded 副本）。剩餘風險是「有人加新格式要求但忘改測試」，這只能靠 checklist 而非純技術手段覆蓋。
- 否決方案：靠「大家記得同步」但無明確觸發機制——已被工程師點名為不可持續做法。

## `_gate_lint` 三個 return 點全數加 `[lint]` 前綴——`"[lint] ruff 缺失，略過 lint 閘門"`、`f"[lint] {name} 未過：..."`、`"[lint] ruff OK"`。
- 時間：2026-06-15 10:59
- 理由：失敗訊息比成功訊息更需要標籤；只改 "ruff OK" 一條會讓最關鍵的失敗路徑反而漏標，前綴意義喪失一半。
- 否決方案：只改成功 return——失敗路徑無標籤，與「一眼辨層」目標矛盾。

## `_gate_collect_without_sdk` 與 `_gate_tests` 各自單一 return，加 `"[collect] "` / `"[test] "` 前綴於 output str 頭部即可；bool 邏輯不動。
- 時間：2026-06-15 10:59

## CI test job 新增獨立 step，name 為 `"Collect tests"`，指令 `python -m pytest --collect-only -q`，env `TI_SANDBOX: "0"`，不帶 `--cov` 任何參數，不加 `continue-on-error`。
- 時間：2026-06-15 10:59
- 理由：`--cov` 在 collect 階段無意義且製造噪音；`continue-on-error` 會吃掉 exit 2，破壞 fail-fast 目的。
- 否決方案：在 collect step 沿用 `--cov=studio` 旗標——徒增噪音且不影響收集結果。

## 新 "Collect tests" step 置於現有 "Run tests" step 正前方；GitHub Actions 預設行為（非 `continue-on-error`）保證 collect exit 2 時 "Run tests" 不會執行。
- 時間：2026-06-15 10:59

## 守護測試以 `yaml.safe_load` 解析 ci.yml 後，鎖定 `jobs['test']['steps']` 清單，不全檔掃 `--collect-only`。
- 時間：2026-06-15 10:59
- 理由：sandbox-test job 的 L193 已含 `--collect-only`；全檔掃會被 sandbox job 假綠通過，條件 (a) 形同虛設。
- 否決方案：全檔掃「任意 step 含 --collect-only」——沙箱 job 會假綠，守護失效。

## 守護測試定位 step 以 **step name 字串搜尋**取 index，斷言 `collect_idx < run_idx`；禁止硬寫 index 常數。
- 時間：2026-06-15 10:59
- 理由：日後若在兩者間插入其他 step，硬常數會假綠；字串比對不受插入影響。
- 否決方案：`steps[N]` 硬常數——結構變動後靜默假綠，與守護目的相反。

## 交付順序明確為「#1（CI yaml）可先合併；#3（守護測試）須待 #1 合入後才能驗紅/綠」；#2（autopilot 標籤）無依賴可隨時交。
- 時間：2026-06-15 10:59
- 理由：「三 diff 可並行」是開發並行，不是驗收並行；QA 的守護測試需要真實 yaml 結構才能驗綠，誤解順序會造成假通過。

## 守護測試同時斷言 collect step 與 run step 都不帶 `continue-on-error: true`；任一帶上即測試紅。
- 時間：2026-06-15 10:59
- 理由：`continue-on-error` 會靜默吸收 exit 2，使獨立步驟的失敗中斷能力形同虛設。

## 零新增執行期依賴——禁止引入 `pytest-pylint`、`pytest-custom-exit-code` 或任何把 lint 嵌入 pytest 的插件；守護測試用標準庫 `yaml`（`pyyaml` 若已在 dev-deps 中則可用，否則換 `json`/手動解析）。
- 時間：2026-06-15 10:59

## sandbox-test job 不動，此次變更範圍僅限 `test` job。
- 時間：2026-06-15 10:59

## 設計文件留存一條演進路徑——「若未來 orchestrator 需機器解析閘門層級（如自動決策），改 return `dict` 並加 `level` 欄位取代前綴字串」；目前純文字前綴已足需求，此路徑屬有意識取捨非疏漏。
- 時間：2026-06-15 10:59

## 版本基準定為 `0.14.4`，三端（本地 venv / CI / pre-commit）對齊此版本。
- 時間：2026-06-15 12:58
- 理由：本地 `.venv/bin/ruff` 實測為 `0.14.4`；CI workflow 已在三處明確 `pip install "ruff==0.14.4"`，並有 comment 說明釘版意圖——基準已存在，缺口僅 pre-commit。
- 否決方案：`0.15.12`——工程師實測的是系統 ruff 而非 venv ruff，數據來源不同環境，不採信為基準。

## 將 `.pre-commit-config.yaml` 的 `rev: v0.6.9` 改為 `rev: v0.14.4`，消除唯一缺口。
- 時間：2026-06-15 12:58
- 理由：pre-commit 是三端中唯一跑舊版（0.6.9）的環節；其餘兩端已對齊，改一個 rev 即關閉分歧。
- 否決方案：降本地與 CI 到 0.6.9——需 reformat 9 檔並改動 CI 三處已鎖的 pin，侵入範圍大且方向逆行。

## 將 `pyproject.toml` 的 `ruff>=0.6` 改為 `ruff==0.14.4`（精確鎖），與 CI 既有 pin 一致。
- 時間：2026-06-15 12:58
- 理由：CI 已精確 pin，pyproject 維持寬鬆上界是唯一可導致本地再次漂移的入口；對齊即消除漂移根因。
- 否決方案：`ruff>=0.14.4,<0.15`——minor-bounded 仍允許 patch 漂移，過去正是「無上界」造成現況，不值得引入殘餘風險。

## 變更範圍嚴格限定 `.pre-commit-config.yaml` 與 `pyproject.toml` 兩檔，驗收時 `git diff --name-only` 必須僅此兩行，禁止 source/test 檔進入 diff。
- 時間：2026-06-15 12:58

## 驗收信號三路獨立跑——① `ruff check .`（lint rules）、② `ruff format --check .`（格式）、③ `pytest --collect-only -q`（收集 2590）——不合併指令。
- 時間：2026-06-15 12:58
- 理由：高級工程師正確指出 format-only 不覆蓋 lint；跨版本可能新增預設 lint rule，需獨立驗收。

## 驗收前須實跑 `pre-commit run ruff-format --all-files` 確認 0 檔被改寫，不以推理替代實跑。
- 時間：2026-06-15 12:58
- 理由：「版本升後 9 檔自動合格」是假設，高級工程師要求實跑佐證；本批交付前必須補這個訊號，否則假設未被驗證。

## CI 安裝來源不需額外修改，已核可。
- 時間：2026-06-15 12:58
- 理由：CI workflow 三處均為 `pip install "ruff==0.14.4"`，高級工程師的疑慮在現況下已不成立；不引入額外改動。

## 不引入任何新執行期依賴，uv lock 統一鎖檔列為有意識保留的未來演進路徑，現階段不動。
- 時間：2026-06-15 12:58

## 需求已在 HEAD 完整落地，不動工、不補 code——驗收訊號三路實跑均綠（ruff format/check 全過；pytest 實際 8 passed，parametrize 展開 args_drift 三 case；git diff HEAD 空）。
- 時間：2026-06-15 13:40
- 理由：工程師實跑確認 8 passed（設計草稿誤載 5），數字以實跑為準，驗收文件同步修正。
- 否決方案：「湊工作量補 code」——在 HEAD 已落地的狀態下新增程式只會製造不必要的 diff，提高後續維護成本。

## spy 接縫選 patch.object(wraps=real_fn)，配合 call_count + 真實 I/O 行數雙層斷言。
- 時間：2026-06-15 13:40
- 理由：wraps 保留真實行為，call_count 攔截執行次數；若只有 mock 層，wraps 實作有缺時副作用可能雙跑而 call_count 仍==1；雙層是必要冗餘，非過度設計。
- 否決方案：MagicMock 純替換——吞副作用，call_count==1 假綠，無法驗證真實 I/O。否決: pytest-idempotent——引入第三方依賴，違反現有「不引入新執行期依賴」原則。

## 注毒（cache.put POISON）在 spy 安裝前執行，隔離 call_count 不污染。
- 時間：2026-06-15 13:40
- 理由：注毒若在 spy 後執行，put.call_count 會含注毒呼叫，須手動扣除，增加斷言脆性與誤判風險。

## 冪等工具測試採「注毒 → 執行 → 結果≠毒值 AND put.call_count==0」三步驗證，缺任一斷言不算數。
- 時間：2026-06-15 13:40
- 理由：僅斷言結果正確不能排除「剛好快取命中返回真實值而非毒值」；僅斷言 call_count==0 不能排除「進了去重路徑但沒寫快取」；兩者互補才能閉合漏洞。

## 反向黑樣本以 parametrize 覆蓋三類 args 漂移（extra_space / extra_key / value_type_drift），斷言行數==2，docstring 明載「翻紅＝限制被解除，修掉須同步改測試」。
- 時間：2026-06-15 13:40
- 理由：比隱式註解能真正逼後人同步評估，若有人直接把斷言改成 ==3 讓 CI 綠，code review 可以介入，但至少文字約定是顯式的。
- 否決方案：隱式記錄限制（只留 comment）——後人不一定讀，且 CI 無法感知。

## 三類測試分三檔，模組邊界對齊測試意圖（dedup/idempotent/args_drift），而非對齊生產模組。
- 時間：2026-06-15 13:40
- 理由：合一後新增 case 容易干擾鄰近意圖，分檔讓 pytest -k 篩選與 CI 分組更直觀。
- 否決方案：合一單檔——意圖混雜，六個月後難以判斷某條測試屬於哪個設計契約。

## 測試只依賴 tools.execute_deduped / DedupCache 公開介面，不 mock 內部 dedup_key 推導（黑箱測試）。
- 時間：2026-06-15 13:40
- 理由：白箱測試耦合內部實作，dedup_key 演算法日後改版（如語意正規化）會無謂崩壞測試，維護成本高。

## 高級工程師提出的三項後續觀察點列為技術債，不阻擋本次驗收——①docstring 警告可升級為 CI linting rule；②行數斷言旁補 comment 說明「這個 2 是哪兩行」；③三檔重複 fixture 若增多可集中到 conftest.py。
- 時間：2026-06-15 13:40
- 理由：三點均屬長期可維護性改善，現況不構成正確性風險，強行納入本次範圍反而擴大 diff、違反「範圍克制」原則。

## 版本基準 — pyproject.toml 鎖定 ruff==0.14.4 為唯一合法執行環境，驗收訊號必須在此版本下產生。
- 時間：2026-06-15 13:59
- 理由：本地 0.15.12 的「0 需 reformat」是假訊號，不得作為結案依據。

## 本地執行環境 — 工程師須在專案 venv 內執行 ruff（pip install ruff==0.14.4 至 venv），或改用 uvx ruff@0.14.4 隔離執行；禁止 global pip install 覆蓋系統版本。
- 時間：2026-06-15 13:59
- 理由：全域降版會把本地環境變成第四個版本來源，正好違反「三端一致」原則。
- 否決方案：直接 global pip install ruff==0.14.4——副作用污染本地，與三端一致目標相悖。

## 修復範圍 — 格式化範圍鎖定 tests/ 目錄；studio/ 生產代碼任何格式改動均超出本任務範圍，須拒絕。
- 時間：2026-06-15 13:59

## 條件性動工 — 第一步跑 ruff format --check tests/（0.14.4 版本環境）；exit 0 → 空 diff 結案，不執行格式化；exit 非 0 → ruff format tests/ 一次收尾。
- 時間：2026-06-15 13:59
- 否決方案：無條件 reformat——若 CI 版本已判 0，多出無謂 diff 污染 git 歷史。

## 三端版本釘死 — pyproject.toml [tool.ruff] 填 0.14.4（pip 格式）；CI workflow 填 ruff==0.14.4；.pre-commit-config.yaml 的 rev 填 v0.14.4（git tag 格式，需加 v 前綴）；三者語義對齊但格式不同，工程師須分別確認，不得混用。
- 時間：2026-06-15 13:59
- 理由：rev 是 git tag，pip 是套件版本號，格式不同但版本語義相同；填錯格式會靜默失敗。

## 驗收訊號 #3 重新定義 — 改為在專案 venv 下執行 python -c "from studio import secure_write; print(secure_write.__file__)"，確認模組路徑指向 studio/secure_write.py；此訊號驗的是「匯出路由正確」，不是格式，須明確標注執行路徑（cd 至 repo root）。
- 時間：2026-06-15 13:59
- 理由：原「無例外」定義過寬，格式化不影響此訊號，幾乎永遠假綠，失去鑑別力。
- 否決方案：維持原 python3 -c "from studio import secure_write" 定義——驗別力不足，無法排除假綠。

## 驗收四訊號執行環境統一 — 四訊號全在同一 venv、同一 repo root 下連續執行，禁止跨環境混跑後拼湊結果；輸出須可回溯（貼完整 terminal 輸出或 CI log 連結）。
- 時間：2026-06-15 13:59

## 不改任何邏輯或 __init__.py — secure_write 匯出已在 HEAD 落地，本任務唯一合法改動是格式（空白/換行）；任何邏輯性改動視為範圍外，退回 PM 重新定義。
- 時間：2026-06-15 13:59

## **`.strip()` 非對稱行為定性為「呼叫者契約」，不修正**
- 時間：2026-06-15 14:58
- 理由：`_is_duplicate` 的設計是「防禦性讀取已存資料（strip stored title）、信任呼叫者傳入乾淨 title」；autopilot 在 LLM 輸出解析後已做 trim，傳入時不會帶尾端空白。補測試的目的是**鎖定現狀**（帶空白的新 title ≠ 精確等值 ⇒ 通過），絕不改成兩邊都 strip，那會破壞③契約。
- 否決方案：「改成 `title.strip() == t["title"].strip()`」——改動③契約，會讓原本刻意帶空白的呼叫方靜默改變行為，超出本任務範圍。

## **層②計數來源鎖定為 `_pending_titles()`（pending + in_progress），done 任務天然排除**
- 時間：2026-06-15 14:58
- 理由：`_filter_pending_duplicates` 在 L695 以 `_pending_titles()` 作為 `existing_titles`；`_count_subsystem_coverage` 只是計數傳入 list，不自行過濾狀態——過濾在來源做。所以「K=3 done + K=1 pending」有效計數為 1，放行，正確。
- 否決方案：在 `_count_subsystem_coverage` 內部加狀態過濾——函式職責是「統計傳入標題」，把狀態過濾移進去會破壞呼叫者可測試的純函式性，責任放錯地方。

## **層②硬擋精確切點：`coverage[s] >= 3` → K=2 放行、K=3 硬擋；測試斷言矩陣須覆蓋邊界三格**
- 時間：2026-06-15 14:58
- 否決方案：用 `> k` 作比較——這會把硬擋推到 K=4 才觸發，讓 K=3 的任務靜默塞入，違反常數命名語義。

## **加啟動期不變式斷言 `assert AUTOPILOT_SUBSYSTEM_MAX < AUTOPILOT_SUBSYSTEM_MAX_PENDING`，放在 `config.py` 常數定義區塊末尾**
- 時間：2026-06-15 14:58
- 理由：兩值目前分散無關聯，任何人調高 SUBSYSTEM_MAX 而忘改 MAX_PENDING，軟門檻就超過硬門檻，整個非對稱設計靜默失效。成本一行、防護真實。
- 否決方案：只靠文件說明——文件不執行，日後改 config 的人不一定讀 ARCHITECTURE.md。

## **層②職責表述修正為「同時負責語意過濾（Jaccard）與數量硬擋（子系統計數）」，設計記錄刪除「兩層不重疊」誤導措辭**
- 時間：2026-06-15 14:58
- 理由：層①②在「數量」上確實有重疊（①軟、②硬），但分層目的不同：①是 prompt 行為引導，②是進場強制拒收。「不重疊」說法不準，日後改數量邏輯時容易只改一邊。

## **格式化（Issue 1）、SDK collect 閘門（Issue 2）在當前 HEAD 已綠，工程師執行 AC 驗收指令確認後即可結案，不製造額外改動**
- 時間：2026-06-15 14:58

## **本任務以零 commit 結案**
- 時間：2026-06-15 16:19
- 理由：工程師實跑確認 `ruff format --check .` exit 0（299 files already formatted）、`pytest test_task1_retry_doc.py` 11 passed、`git status` 乾淨；需求所附 10-file 清單為修前快照，不作為行動觸發器。
- 否決方案：「為顯示有產出而補記 ARCHITECTURE.md 措辭」——doc 測試已全綠，觸發條件不存在，動筆只會引入新風險。

## **驗收四訊號指令改為逐條執行、各自記錄退出碼，禁止 `&&` 串接**
- 時間：2026-06-15 16:19
- 理由：`&&` 在前訊號失敗時短路，後續訊號的退出碼無從記錄，違反「退出碼逐一可追溯」的鐵則。本次全綠沒踩到，但規則要在全綠時就鎖定，不能靠運氣。
- 否決方案：「`&&` 串接便於一行執行」——便利性不敵可追溯性，特別是此設計的核心價值就是「不靠感覺、靠碼」。

## **`architecture review`（第三訊號）pass 條件客觀化：「`test_task1_retry_doc.py` 所有斷言的目標字詞均可在 ARCHITECTURE.md retry 小節中以 `grep` 定位」**
- 時間：2026-06-15 16:19
- 理由：高級工程師指出第三訊號為人工判斷，無客觀通過條件，是可追溯性缺口。改為可機器驗證的 grep 比對，消除審查者主觀分歧的空間。
- 否決方案：「由審查者人工確認三要素齊備」——依賴個人判斷無法跨人員重現，且已有 pytest 守護，人工層不應更寬鬆。

## **「ruff 三端版本同步」待辦以結構化行 `核心改動: ruff 版本三端同步（pyproject / pre-commit / CI 須同為 0.14.4）` 路由至核心 backlog，不口頭記錄**
- 時間：2026-06-15 16:19
- 理由：高級工程師指出僅口頭記錄的待辦會消失；依 CLAUDE.md 架構鐵則，核心框架改動以 `核心改動:` 標記路由，讓 autopilot 接手追蹤，不依賴本次討論存活。
- 否決方案：「本次順手查三端版本即可」——查完若不落 backlog 仍會消失；且三端同步屬工具鏈維護，是核心 repo 範疇，不應混入本 PR。

## 新建 studio/release_smoke.py，公開純函式 check_body(body: str) -> None 與 CLI __main__；check_body 只 raise ValueError(reason)，__main__ 負責 catch + stderr + sys.exit(1)。
- 時間：2026-06-15 17:34
- 理由：副作用邊界分離——check_body 可在測試中直接 pytest.raises(ValueError) 而不需 mock sys.exit；CLI 行為不變，非 CLI 呼叫者（未來的 programmatic 整合）亦可安全呼叫。
- 否決方案：check_body 直接呼叫 sys.exit(1)——單元測試須 pytest.raises(SystemExit)，且函式對非 CLI 呼叫者完全不可復用。

## check_body 判定條件用 if not extract_breaking_block(body)（即 None 視為失敗）；依 release_note.py L92–93 `return body or None` 的已知合約，空字串不可能回傳，is not None 與 not result 等價，選 not result 作防禦性寫法並加行內注釋標明 contract 依賴。
- 時間：2026-06-15 17:34
- 理由：高工指出的空字串風險真實存在於「contract 不明」時，加注釋讓未來讀者不必重查 SSOT 即可理解。

## release body 以環境變數 BODY 傳入 release_smoke.py（workflow 中 env: BODY: ${{ steps.body.outputs.body }}），__main__ 優先讀 os.environ.get("BODY")，fallback sys.stdin.read()。
- 時間：2026-06-15 17:34
- 理由：echo "$BODY" 在多行、含反斜線、-e/-n 開頭時會吞字或誤判旗標；env 傳遞不經 shell 字串解析，最穩。fallback stdin 保留本地管線測試與手動驗收能力（兩種呼叫方式都能用）。
- 否決方案：echo "$BODY" | python——體積大的 release body 含特殊字元時靜默截斷，smoke 拿到殘缺 body 仍可能通過，鑑別力失效。

## 觸發時機選 on: release: types: [published]，不加 retry guard。
- 時間：2026-06-15 17:34
- 理由：release 物件在 published 事件時已就緒，零 race condition；retry guard 增加 YAML 複雜度且掩蓋更深問題。
- 否決方案：push: tags + retry loop——需額外 30 秒 guard，且若 release 從未建立會 retry 到上限才失敗，診斷模糊。

## smoke 判定層只做「非空頂層 Breaking Changes 區塊存在」，不套 outlet_carries_block() 的四要素檢查。
- 時間：2026-06-15 17:34
- 理由：AC#2 契約是「有非空區塊即通過」，四要素是 pre-tag validator 職責；smoke 過嚴會讓 release 因 CHANGELOG 措辭問題被擋，而非發布流程錯誤。
- 否決方案：複用 outlet_carries_block()——四要素語意偵測對 post-publish smoke 過嚴，且把 pre-tag 與 post-publish 兩層職責混入同一函式。

## 版本字串以 pyproject_version()（studio.release_note 已有）為 SSOT，workflow 以獨立 step 取得並輸出至 GITHUB_OUTPUT，版本號可見於 Actions log，供 debug 確認。
- 時間：2026-06-15 17:34
- 否決方案：在 workflow YAML 硬寫版本號或另寫 tomllib 解析——多一份解析邏輯就多一個漂移點。

## YAML 與 release_smoke.py 均不出現任何 Breaking Changes heading 字面值；SSOT 路徑唯一為 extract_breaking_block（其內引用 BREAKING_HEADING）；Task #4 驗收以 grep .github/workflows/ studio/release_smoke.py 確認 0 命中。
- 時間：2026-06-15 17:34
- 否決方案：workflow 中用 --jq '.body | test("## ⚠️ Breaking Changes")'——jq 中硬寫 heading 字面值打破 SSOT，emoji 漂移時 smoke pass 而 pre-tag fail，靜默分歧。

## smoke 失敗診斷輸出為 print(body[:500] or "<empty>", file=sys.stderr)；空 body 時明確印 <empty> 而非空白，避免「看起來沒執行」的誤判。
- 時間：2026-06-15 17:34
- 理由：空 body 是有效的失敗場景（release body 未填），需讓 CI log 可讀。

## 動工前工程師與 workflow 撰寫者先敲定 body 傳遞介面（env BODY + stdin fallback），再各自並行實作 #1、#2、#3；Task #4 在 #2 完成後以 grep 驗收。
- 時間：2026-06-15 17:34
- 理由：介面未對齊時 #4 驗收才發現問題，返工成本最高；先定介面是最便宜的協調點。

