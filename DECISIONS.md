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

