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

## 新增 `.github/workflows/publish-release.yml`，trigger `on: push: tags: ['v*']`，job-level 設 `permissions: contents: write`；不與 `release-smoke.yml` 合併。
- 時間：2026-06-15 18:22
- 理由：建立職責 vs 驗證職責分離；`contents: write` 是 `gh release create` 最小權限需求。

## publish job 分五個 step 依序執行：(1) Verify PAT、(2) Resolve version、(3) Assert tag matches version、(4) Render body、(5) Create release。
- 時間：2026-06-15 18:22
- 理由：把「PAT 未設」攔在最前面，避免前四步成功後才 403，錯誤訊息更直覺。

## Step 1 為 PAT guard：`run: test -n "$GH_TOKEN" || (echo "::error::GH_PAT secret 未設或已過期" && exit 1)`，`env: GH_TOKEN: ${{ secrets.GH_PAT }}`。
- 時間：2026-06-15 18:22
- 理由：PAT 過期靜默炸掉 pipeline 是中風險；guard step 讓錯誤訊息直接指向根因，不是 403 猜測。
- 否決方案：只在 README 備註到期日——文件在緊急時沒人看，guard 在 CI log 裡看得見。

## Step 3 斷言 `github.ref_name == v{pyproject_version()}`，不符即 fail-fast，不建立 release。
- 時間：2026-06-15 18:22
- 理由：tag 與程式碼版本不同步是上線事故根因；最便宜的攔截點是 release 建立前。

## 新增 `scripts/publish_release.py`（薄包裝，< 15 行），讀 `CHANGELOG.md` → 呼叫 `render_tag_notes(changelog_text, pyproject_version())` → 寫 `body.md`；workflow step 4 僅執行 `python scripts/publish_release.py`。
- 時間：2026-06-15 18:22
- 理由：inline Python in YAML 無法被 pytest 直接 import 測試，守護測試只能掃 YAML 結構，意義打折；抽成腳本後 task #4 可直接 import 並跑 dry-run，測試深度從「結構存在」升至「邏輯正確」。
- 否決方案：`python -c "..."` 或 `run: | 多行 python`——邏輯不可測，且 shell quoting 仍有潛在風險。

## `scripts/publish_release.py` 作為入口點時設 `set -euo pipefail`（或 Python 層 raise 即退），確保 render 失敗後 `body.md` 不殘留舊內容供 step 5 誤讀。
- 時間：2026-06-15 18:22
- 理由：step 4 失敗後 step 5 若仍執行會讀到舊版 body.md，file mode 本身不防這點；`set -euo pipefail` 是最小成本的冪等性保障。

## Step 5 執行 `gh release create ${{ steps.version.outputs.tag }} -F body.md`，不帶 `--draft`，狀態直接為 published；`env: GH_TOKEN: ${{ secrets.GH_PAT }}`（PAT 身分建立，才能觸發下游 release:published 事件）。
- 時間：2026-06-15 18:22
- 理由：GITHUB_TOKEN 建立的 release 不觸發下游 workflow（GitHub 防遞迴機制）；PAT 是破解死結的最小侵入方案，smoke workflow 無需改動。
- 否決方案：workflow_dispatch 接力——smoke 改讀 dispatch input 而非真實 release body，破壞「讀真實 body」的守護意義；GitHub App token——設置成本高，不符合當前規模。

## `render_tag_notes` 呼叫傳入字串（`Path('CHANGELOG.md').read_text()`），不傳路徑；`scripts/publish_release.py` 負責讀檔，`studio.release_note` 函式簽名不動。
- 時間：2026-06-15 18:22
- 理由：工程師指出資料流圖的描述歧義，此處釘死介面邊界，避免實作時照抄 bug。

## YAML 與 `scripts/publish_release.py` 內零 Breaking heading 字面值；所有 heading 參照透過 `studio.release_note.BREAKING_HEADING`（或 `extract_breaking_block`）匯入，task #4 grep 驗收維持 0 命中。
- 時間：2026-06-15 18:22

## task #4 守護測試分兩層：(a) YAML 解析層——斷言 `publish-release.yml` 存在 `-F body.md` step、`GH_TOKEN: ${{ secrets.GH_PAT }}`（非 GITHUB_TOKEN）；(b) 邏輯層——直接 import `scripts/publish_release.py` 的 render 邏輯並以 fixture CHANGELOG 跑 dry-run，斷言 output 非空且含 Breaking block；mutation 把 PAT 改回 GITHUB_TOKEN 必須讓 (a) 轉紅。
- 時間：2026-06-15 18:22
- 理由：高工指出純 YAML 掃描脆弱（結構重排假紅）；兩層互補——結構變動由 (a) 抓，邏輯迴歸由 (b) 抓。

## PR 描述必須明確標注「AC#3 端對端驗收需 `GH_PAT` 先設入 repo secrets；secret 未設前 push tag 會在 step 5 以 403 失敗，屬預期行為而非 bug」，避免驗收工程師誤判。
- 時間：2026-06-15 18:22
- 理由：工程師提醒這是最容易被「誤標綠燈」的盲點，文件成本最低。

## **domain API 為 `render_tag_notes(changelog_text: str, version: str) -> str`**，定義於 `studio/release_note.py`，接收字串，不接觸 I/O，可被 pytest 直接 import 測試。
- 時間：2026-06-15 19:50
- 理由：純函式最易被任意 fixture 覆寫，測試不需要真實檔案系統。
- 否決方案：讓 domain 層接受路徑——I/O 副作用混入 domain，測試需要 fixture 檔案，可測性下降。

## **adapter API 為 `render_release_body(changelog_path=None, version=None) -> str`**，定義於 `scripts/publish_release.py`，負責讀檔並呼叫 `render_tag_notes`；邏輯層守護測試 import 此函式做 dry-run，SSOT 常數測試 import `BREAKING_HEADING`——兩層不混用。
- 時間：2026-06-15 19:50
- 理由：釘住「哪層測試 import 哪個符號」，消除高工指出的命名歧義，避免未來因混用而出現 `AttributeError`。

## **publish-release.yml 的 create release step 必須加 `id: create-release`**；守護測試 `_create_release_step()` 以 `step["id"] == "create-release"` 定位，不用 index 或 name 字串。
- 時間：2026-06-15 19:50
- 理由：高工指出 content-matching 在 step 重排或 run 字串改名時靜默斷鏈（guard 仍綠但已失效）；id 定位語意明確，改名即假紅，有鑑別力。
- 否決方案：以 `"gh release create" in step["run"]` 定位——若 step 加 `if:` 條件或 run 字串重構，guard 仍能過，失去守護意義。

## **body 注入用 `-F body.md`（gh CLI 的 `--notes-file` 縮寫），不用 `--notes "$BODY"`**，且此字串必須出現在 create-release step 的 `run:` 中並被守護測試 `create_uses_file_mode()` 斷言。
- 時間：2026-06-15 19:50
- 理由：工程師確認多行 changelog 經 shell 變數注入會踩跳脫地獄；`-F` 讓 gh CLI 直接讀檔，完全繞開 shell 解析。

## **邏輯層守護測試的 fixture CHANGELOG 必須以 `f"...{BREAKING_HEADING}..."` 動態組出**，不得硬寫 `## ⚠️ Breaking Changes` 字面值。
- 時間：2026-06-15 19:50
- 理由：工程師指出常數一旦改字，硬寫 fixture 靜默過期，乾跑變成在測試一份永遠成功的舊格式。
- 否決方案：fixture 硬寫字面值——BREAKING_HEADING 修改時 fixture 不跟進，dry-run 仍綠但實際 render 會失敗。

## **`main()` 內 `unlink(missing_ok=True)` 緊接 `render_release_body()` 兩行相鄰，中間無任何 early return 或寫檔**；語意為 pre-clean（非原子 rollback）——process 若被 kill 於 unlink 之後，`body.md` 缺失，step 5 fail-fast，靠 CI 重跑恢復；設計不保證原子性。
- 時間：2026-06-15 19:50
- 理由：工程師確認現有順序正確；高工要求語意說清。「不保證原子性」比沉默更誠實，日後偶發假紅時有心理準備。

## **release-smoke 斷言：body 非空 AND 含 `BREAKING_HEADING`**（從 `studio.release_note` import 常數比對，不硬寫字面值）；僅驗「release 存在」不構成 smoke，必須驗 body 結構。
- 時間：2026-06-15 19:50
- 理由：高工指出現有設計只說「讀 body 做 smoke」，無具體斷言；smoke 形同空轉。

## **觸發時序措辭統一為「`release:published` 觸發時 release 物件已就緒，無需主動輪詢」**，不寫「零 race condition」。
- 時間：2026-06-15 19:50
- 理由：高工指出 GitHub API 最終一致性在極端情況有短暫視窗；「已就緒無需輪詢」是正確承諾，「零 race condition」過強。

## 承繼既有決策——**PAT 解鎖觸發死結、tag 經 `env TAG` 防 injection、`release:published` 不改 `workflow_run`、step 3 fail-fast tag 斷言、PR 描述明文標注 GH_PAT 前置**——以上高工與工程師均無異議，維持原案不變。
- 時間：2026-06-15 19:50

## demo command-not-found 真因定位在 OS shell 層，改文件是命中真因，不動 runner `_executable_command` 內部邏輯。
- 時間：2026-06-15 20:19
- 理由：runner 介入前 shell 已報錯，映射層從未執行；改執行層屬誤治。

## 改動範圍鎖定四份使用者面文件（README.md、CHANGELOG.md、ARCHITECTURE.md、CONTRIBUTING.md），但 CHANGELOG.md 內描述舊版行為的歷史記錄段落列為禁動區。
- 時間：2026-06-15 20:19
- 理由：歷史段落改了反而製造不實紀錄；高工指出此邊界需明確。
- 否決方案：全文件無差別替換——CHANGELOG 歷史段語意不同，不應與「指令宣告」同等對待。

## CI workflow（`.github/workflows/*.yml`）預設不動；動工前須確認無 Makefile/just recipe 抄文件 sample command，若有，該 recipe 納入改動範圍。
- 時間：2026-06-15 20:19
- 理由：CI 跑 setup-python 不是 demo 路徑；但若 CI 直接執行文件指令，不動會留不一致，高工指出需核實。

## 替換策略採逐檔精確 Edit，不用批次 sed/perl。
- 時間：2026-06-15 20:19

## 第 1 步 grep 命中清單固化為表格（檔案、行號、原文、預定改動/keep 標記），存檔後給 PM approve；改完跑同一條 grep，兩份輸出比對，作為禁動區核驗依據。
- 時間：2026-06-15 20:19
- 理由：高工指出格式未定義是流程卡點；工程師指出兩份輸出比對優於人腦記憶。

## 執行驗收指令採 `python3 -c "import studio.server"`（即退、不阻塞），整體驗收沿用 `python3 -m pytest tests/docs tests/core/test_runner.py -q`。
- 時間：2026-06-15 20:19
- 理由：工程師指出全啟動會卡住 CI；驗收語意只需確認「指令字串找得到 python3 並能解析入口」，import 錯/依賴缺失為另一問題域，不混判。
- 否決方案：`python3 -m studio.server`（無限制）——會阻塞執行流程。
- 勘誤（第 3 輪）：原宣告 `python3 -c "import studio; python3 -m studio.server --help"` 在 `-c` 字串內巢狀 `python3 -m`，屬非法 Python 語法（SyntaxError, exit=1），且 server 未實作 `--help` 會 timeout；兩重缺陷致連續 2 輪卡關。已實測更正為 `python3 -c "import studio.server"`（exit=0）。

## 驗收「非 command not found」的目標環境定義為「未安裝 python symlink 的 bare macOS Monterey+ 或 Ubuntu 22.04+」，不以 dev 本機為準。
- 時間：2026-06-15 20:19
- 理由：高工指出「本環境過關」不等於使用者環境過關，最小環境需明文定義。
- 否決方案：以 dev 本機為驗收環境——python symlink 可能由 pyenv 提供，非 bare 狀態，不能代表使用者。

## guard 測試正則（`tests/docs/*` 等）若驗「不含裸 python 指令」，其排除條件必須同步涵蓋禁動區字串（`python-dotenv`、```python 圍欄、`python3`），避免文件改完後 guard 反紅。
- 時間：2026-06-15 20:19
- 理由：工程師指出此邊界容易漏，guard 正則本身若不排除禁動區，文件改正確但測試報錯，製造假紅。

## guard 測試與文件改動必須同 batch、同 commit，不允許中間狀態。
- 時間：2026-06-15 20:19

## shebang 不納入本次範圍（現有腳本均為 `env bash`，無 python shebang）。
- 時間：2026-06-15 20:19

## 產物頂端第一行即放「本任務實為驗證型 no-op，無任何寫入操作」結論句，三段證據為佐證而非主體——PM 第一眼掌握任務性質。
- 時間：2026-06-17 20:10
- 理由：避免 PM 讀完整份報告才發現是空包，縮短決策路徑。
- 否決方案：原本「三段證據平鋪、由 PM 自己推導結論」的順序——推導成本不該丟給 PM。

## C 段（渲染行指認）grep pattern 綁死為兩組關鍵字——`criteria` 與 `render`，兩組都需有命中才算「渲染邏輯存在」；命中後輸出含行號與前後 2 行 context，不得只貼行號。
- 時間：2026-06-17 20:10
- 理由：防止「grep 什麼隨便找」的黑箱風險；高工指認為最大設計洞。
- 否決方案：執行端自由發揮 pattern（工程師原本「先 cat 再定 pattern」雖方向對，但未綁死判定邏輯，仍有操作空間）。

## B 段（app.js == HEAD）採 `git diff --stat HEAD -- web/app.js` 先看一行彙整，零差即收工；如需深查再加 `git diff HEAD -- web/app.js`。
- 時間：2026-06-17 20:10
- 理由：高工建議；零差時完整 diff 為空但有 noise，stat 先看更省時。
- 否決方案：直接跑完整 `git diff HEAD`（零差時多餘輸出）。

## 範圍外清單必須跑 `git log --oneline -n 5` + `git show --stat <sha>` 撈出實際「不相關 commit」與其檔名/SHA，不接受「@ 4572a49」之類的舉例式標註。
- 時間：2026-06-17 20:10
- 理由：PM 拿到清單要判斷是否 revert，必須是實資料不是猜測。
- 否決方案：沿用研究快照的範例 SHA。

## 範圍外清單另增一條「流程守則給 PM」——下次需求 doc 須附 `git rev-parse HEAD` 與 `git status --short` 自驗，避免舊快照重演。
- 時間：2026-06-17 20:10
- 理由：高工指出此為流程債，趁現在順手記。
- 否決方案：留待下個 task 處理（會重演同樣卡點）。

## 沿用：任務性質 = 驗證型 no-op，全程 read-only（禁 `git restore` / `git reset` / `git rm` / `git checkout` 寫入子命令）。
- 時間：2026-06-17 20:10
- 理由：既有紀律「可逆性優先」「本環境過關 ≠ 使用者環境過關」延伸。
- 否決方案：硬做清理（會引入破壞性操作，違反可逆性）。

## 沿用：技術選型 = 純 git CLI + grep，無 wrapper、無腳本框架。
- 時間：2026-06-17 20:10
- 理由：為 no-op 任務增加工具面是過度設計。
- 否決方案：包成 `verify.sh` 腳本（一次性驗收物不需入庫）。

## 沿用：介面格式 = 單一 markdown 區塊，依 A/B/C 三段、每段附「執行指令 + 預期輸出 + 實測輸出」三欄；明註「一次性驗收物、不為後續自動化保留介面」。
- 時間：2026-06-17 20:10
- 理由：高工次要建議；防止下次被誤當 API 接 CI。
- 否決方案：產 JSON / 結構化檔（為 no-op 投資未來介面是 YAGNI）。

## 沿用：範圍邊界 = 只處理 staged/working tree 層級；已 commit 進 origin/main 的 INVENTORY_task1.md 等明確排除。
- 時間：2026-06-17 20:10
- 理由：「清 staged」無法解決「已落庫」，強做會越界到 revert/removal 議題。
- 否決方案：併入本任務一併移除已 commit 檔（範圍爆炸，PM 未授權）。

## 沿用：驗收逐項精確、任一失敗即停回報 PM；驗收標準綁死 PM 提出的四項（status 空、app.js diff 空、渲染行可指認、範圍外清單交付）。
- 時間：2026-06-17 20:10
- 理由：既有「逐檔精確」紀律的對偶；不允許「A 失敗 B/C 過就先交」。
- 否決方案：部分通過即出貨（會埋未爆彈）。

## 沿用：模組切分 = A 工作樹乾淨 / B app.js == HEAD / C 渲染邏輯存在，三段各自可單獨重跑。
- 時間：2026-06-17 20:10
- 理由：便於 PM/QA 抽查任一段；對應三個不同的失敗模式。
- 否決方案：三段合成單一複合指令（失敗時不知哪一段炸）。

## 產物頂端置「本任務實為驗證型 no-op，無寫入」結論句，三段證據為佐證；PM 第一眼掌握性質。
- 時間：2026-06-17 20:10
- 理由：避免讀完整份才發現是空包。
- 否決方案：三段平鋪、由 PM 自推結論（推導成本不該丟給 PM）。

## C 段 grep pattern 綁死為 `criteria` + `render` 兩組關鍵字，皆需命中；命中後輸出含行號與前後 2 行 context。
- 時間：2026-06-17 20:10
- 理由：高工指為最大設計洞；防「grep 隨便找」黑箱。
- 否決方案：執行端自由發揮 pattern（工程師原案未綁死判定邏輯）。

## B 段採 `git diff --stat HEAD -- web/app.js` 先看彙整，零差即收。
- 時間：2026-06-17 20:10
- 理由：高工建議；零差時完整 diff 為空但有 noise。
- 否決方案：直接跑 `git diff HEAD` 完整輸出。

## 範圍外清單須由 `git log --oneline -n 5` + `git show --stat <sha>` 撈實 commit 與檔名/SHA，不接受舉例式標註。
- 時間：2026-06-17 20:10
- 理由：PM 要據此判斷是否 revert，必須是實資料。
- 否決方案：沿用研究快照的範例 SHA。

## 範圍外清單增「流程守則」——下次需求 doc 附 `git rev-parse HEAD` + `git status --short` 自驗。
- 時間：2026-06-17 20:10
- 理由：高工指出為流程債，趁現在記。
- 否決方案：留待下個 task（會重演同樣卡點）。

## 介面格式明註「一次性驗收物、不為後續自動化保留介面」。
- 時間：2026-06-17 20:10
- 理由：高工次要建議；防被誤當 API 接 CI。
- 否決方案：產結構化檔預留未來介面（YAGNI）。

## 沿用：任務 = 驗證型 no-op，全程 read-only，禁 `restore` / `reset` / `rm` / `checkout` 寫入子命令；技術選型 = 純 git CLI + grep，無 wrapper；介面 = 單一 markdown 區塊、三段「指令/預期/實測」；範圍 = 只處理 staged/working tree，已 commit 不相關檔排除；驗收 = 逐項精確、任一失敗即停，綁死 PM 四項標準；三段模組各自可單獨重跑。
- 時間：2026-06-17 20:10
- 理由：既有紀律延伸；不再展開取捨避免冗述。

## 任務性質 = 驗證型 no-op；技術選型 = 純 git CLI + grep，無 wrapper；介面 = 單一 markdown 區塊、三段「指令/預期/實測」；範圍 = 只處理 staged/working tree，已 commit 不相關檔排除；驗收 = 逐項精確、任一失敗即停，綁死 PM 四項；模組 = A/B/C 三段各自可單獨重跑；不變性 = 全程 read-only、禁 `restore`/`reset`/`rm`/`checkout` 寫入子命令。
- 時間：2026-06-17 20:10

## 沿用：任務性質 = 驗證型 no-op。
- 時間：2026-06-17 20:10

## 沿用：技術選型 = 純 git CLI + grep，無 wrapper。
- 時間：2026-06-17 20:10

## 沿用：介面 = 單一 markdown 區塊，三段「指令/預期/實測」分欄。
- 時間：2026-06-17 20:10

## 沿用：範圍 = 只處理 staged/working tree 層級，已 commit 進 origin/main 的不相關檔排除。
- 時間：2026-06-17 20:10

## 沿用：驗收 = 逐項精確、任一失敗即停回報 PM，綁死 PM 四項標準。
- 時間：2026-06-17 20:10

## 沿用：模組 = A 工作樹乾淨 / B app.js == HEAD / C 渲染邏輯存在，三段各自可單獨重跑。
- 時間：2026-06-17 20:10

## 沿用：不變性 = 全程 read-only，禁 `restore` / `reset` / `rm` / `checkout` 寫入子命令。
- 時間：2026-06-17 20:10

## 產物頂端置「本任務實為驗證型 no-op，無任何寫入操作」結論句，三段證據為佐證；PM 第一眼掌握任務性質。
- 時間：2026-06-17 20:10
- 理由：避免讀完整份才發現是空包，縮短決策路徑。
- 否決方案：三段平鋪、由 PM 自推結論（推導成本不該丟給 PM）。

## C 段 grep pattern 綁死為 `criteria` + `render` 兩組關鍵字，皆需命中；命中後輸出含行號與前後 2 行 context，不得只貼行號。
- 時間：2026-06-17 20:10
- 理由：高工指為最大設計洞；防「grep 隨便找」黑箱化為「為了過而過」。
- 否決方案：執行端自由發揮 pattern（工程師原案「先 cat 再定」未綁死判定邏輯）。

## B 段採 `git diff --stat HEAD -- web/app.js` 先看彙整，零差即收；非零差再跑完整 diff。
- 時間：2026-06-17 20:10
- 理由：高工建議；零差時完整 diff 雖空但有 noise，stat 先看更省時。
- 否決方案：直接跑完整 `git diff HEAD -- web/app.js`（零差時多餘輸出）。

## 範圍外清單須由 `git log --oneline -n 5` + `git show --stat <sha>` 撈出實際「不相關 commit」與其檔名/SHA，不接受舉例式標註。
- 時間：2026-06-17 20:10
- 理由：PM 須據此判斷是否 revert，必須是實資料不是猜測。
- 否決方案：沿用研究快照的範例 SHA（如「@ 4572a49」）。

## 範圍外清單增一條「流程守則給 PM」——下次需求 doc 須附 `git rev-parse HEAD` 與 `git status --short` 自驗，避免舊快照重演。
- 時間：2026-06-17 20:10
- 理由：高工指出此為流程債，趁現在順手記下；不修下個 task 會重演同樣卡點。
- 否決方案：留待下個 task 再處理。

## 沿用既有設計——任務性質 = 驗證型 no-op；技術選型 = 純 git CLI + grep、無 wrapper；介面 = 單一 markdown 區塊、三段「指令/預期/實測」；範圍 = 只處理 staged/working tree、已 commit 不相關檔排除；驗收 = 逐項精確、任一失敗即停，綁死 PM 四項標準；模組 = A/B/C 三段各自可單獨重跑；不變性 = 全程 read-only，禁 `restore` / `reset` / `rm` / `checkout` 寫入子命令。
- 時間：2026-06-17 20:10
- 理由：既有紀律延伸，PM 已核可方向；不再展開取捨避免冗述。

## C 段 grep pattern 綁死為 `criteria` + `render` 兩組關鍵字，皆需命中才算「渲染邏輯存在」；命中後輸出含行號與前後 2 行 context，不得只貼行號。
- 時間：2026-06-17 20:10
- 理由：高工指為最大設計洞；防「grep 隨便找」黑箱化為「為了過而過」捷徑。
- 否決方案：執行端自由發揮 pattern（工程師原案「先 cat 再定」未綁死判定邏輯，仍有操作空間）。

## B 段採 `git diff --stat HEAD -- web/app.js` 先看一行彙整，零差即收；非零差再跑完整 `git diff`。
- 時間：2026-06-17 20:10
- 理由：高工建議；零差時完整 diff 雖空但有 noise，stat 先看更省時。
- 否決方案：直接跑完整 `git diff HEAD -- web/app.js`（零差時多餘輸出）。

## 沿用既有設計——任務性質 = 驗證型 no-op；技術選型 = 純 git CLI + grep、無 wrapper；介面 = 單一 markdown 區塊、三段「指令/預期/實測」分欄；範圍 = 只處理 staged/working tree 層級、已 commit 進 origin/main 的不相關檔排除；驗收 = 逐項精確、任一失敗即停回報 PM，綁死 PM 四項標準；模組 = A 工作樹乾淨 / B app.js == HEAD / C 渲染邏輯存在，三段各自可單獨重跑；不變性 = 全程 read-only，禁 `restore` / `reset` / `rm` / `checkout` 任何寫入子命令。
- 時間：2026-06-17 20:10
- 理由：既有紀律延伸，PM/工程師/高工三方已核可方向；不再展開取捨避免冗述。

## 技術選型沿用既有 Ruff + pre-commit + CI，規則來源維持 `pyproject.toml` 單一配置。
- 時間：2026-06-18 23:07
- 理由：降低規則分歧與維護成本。
- 否決方案：自製 parser、額外掃描器、平行 lint 規則集。

## 不基於「`ruff --fix` 可能不停下」直接改 hook；先做一次 pre-commit 實測確認 hook 修改檔案後是否阻擋提交。
- 時間：2026-06-18 23:07
- 理由：避免用錯誤假設堆補丁。
- 否決方案：未驗證就新增第二套防線。

## 若實測確認 pre-commit 已會因自動修正而失敗，實作範圍只補文件或驗收紀錄，不改現有 hook。
- 時間：2026-06-18 23:07
- 理由：現有 CI 已有 `ruff check .` 與 `ruff format --check .` 非修正式 gate。
- 否決方案：為了形式安全而增加重複檢查。

## 若實測發現 hook 會吞掉自動修正，才最小化補強為「修正式 Ruff 後接非修正式 Ruff check」。
- 時間：2026-06-18 23:07
- 理由：保留自動修正體驗，同時確保提交不可靜默通過。
- 否決方案：拆出新 workflow、改用 wrapper script、擴大 CI 架構。

## 依賴方向固定為工具設定驅動流程，業務程式碼不得依賴 lint/pre-commit 實作細節。
- 時間：2026-06-18 23:07
- 理由：讓未來替換 hook 或升級 Ruff 可逆。

## 新增獨立 CI job `deploy-test`，既有 `lint`、`test`、`sandbox-test` 不改。
- 時間：2026-06-19 03:02
- 理由：明確形成 deploy merge gate。
- 否決方案：不併入矩陣、不用 path filter。

## `deploy-test` job id/name 固定不改名，作為 GitHub required check 介面。
- 時間：2026-06-19 03:02
- 理由：保護規則依賴 check 名稱，穩定性優先。

## README 記錄 required check 為 `deploy-test`，但實際 ruleset 設定時需以 GitHub Actions 顯示名稱確認一次。
- 時間：2026-06-19 03:02
- 理由：README 只是文件，真正擋 merge 仍靠 branch protection/ruleset。

## branch protection/ruleset 必須手動或以 IaC 同步加入 `deploy-test` required check。
- 時間：2026-06-19 03:02
- 理由：CI 檔新增 job 不等於自動啟用 merge gate。

## `deploy-test` 使用 Python `3.12`、`TI_SANDBOX=0`、`timeout-minutes: 10`。
- 時間：2026-06-19 03:02
- 理由：對齊部署實跑環境與既有 CI 沙箱策略。

## 依賴安裝沿用現有 `test` job 的最小 pip 依賴集合。
- 時間：2026-06-19 03:02
- 否決方案：不新增 marker、xdist、專用 wrapper 或額外套件。

## 測試指令固定為 `python -m pytest tests/deploy -q`。
- 時間：2026-06-19 03:02
- 理由：保留 pytest 原生失敗語意，包含 tests 被清空時 exit code 5 直接紅燈。

## 不設 `continue-on-error`，不設 workflow/job path filter。
- 時間：2026-06-19 03:02
- 理由：required check 要穩定出現且不可假綠。

## 接受 deploy 測試被矩陣全測與 `deploy-test` 重複跑一次。
- 時間：2026-06-19 03:02
- 理由：放棄省 CI 分鐘，換取清楚、可要求的 merge gate。

## 實作範圍只限 `.github/workflows/ci.yml` 與 `README.md`。
- 時間：2026-06-19 03:02
- 否決方案：不改業務程式碼、不改 `tests/deploy/` 測試內容。

## 變更範圍限於 `studio/providers.py` 與 `tests/core/test_providers.py`。
- 時間：2026-06-19 05:01
- 理由：缺陷在 `CodexExpert` 子程序生命週期，無需動 runner/orchestrator。

## 使用 Python 標準庫 `asyncio`、`os`、`signal`，不引入 `psutil`。
- 時間：2026-06-19 05:01
- 否決方案：不新增跨平台 process-tree 抽象。

## `CodexExpert` 持有單一 `self._proc`，代表目前執行中的 `codex exec`。
- 時間：2026-06-19 05:01

## 同一個 `CodexExpert` instance 不支援並發 speak；若已有執行中 proc，新的執行必須明確失敗，不可 silent overwrite。
- 時間：2026-06-19 05:01
- 理由：保護生命週期引用正確性，放棄同角色並發換取可預期停止語意。

## `_run_codex()` 在 `create_subprocess_exec()` 成功後立即設定 `self._proc = proc`。
- 時間：2026-06-19 05:01

## `_run_codex()` 的 `finally` 只在 `self._proc is proc` 時清空引用。
- 時間：2026-06-19 05:01
- 理由：避免舊輪收尾誤清新輪 proc。

## `_run_codex()` 必須處理取消路徑；若 coroutine 被取消且 proc 仍執行，需終止並回收後再重新拋出 `CancelledError`。
- 時間：2026-06-19 05:01
- 理由：不依賴外部一定會補呼叫 `stop()`。

## `stop()` 維持 async 且語意為「停止完成」；呼叫 `_terminate(proc)` 後必須 bounded `await proc.wait()`。
- 時間：2026-06-19 05:01

## `stop()` 若等待逾時，必須升級強制 kill，再 bounded 等待回收。
- 時間：2026-06-19 05:01
- 理由：上層使用 `await expert.stop()`，不能只送訊號就返回。

## `_terminate()` 對齊 runner 策略：用 `os.getpgid(proc.pid)` 取得 process group 後送訊號，捕捉 `PermissionError/OSError`，fallback 到 `proc.kill()`。
- 時間：2026-06-19 05:01
- 否決方案：不採用只 `os.killpg(proc.pid, SIGTERM)` 的窄版實作。

## `_terminate()` 保持低階 helper，只負責送訊號；等待、逾時、升級 kill 由 `stop()` 與取消清理路徑負責。
- 時間：2026-06-19 05:01

## 測試使用 fake process 與 monkeypatch，不啟動真 Codex CLI、不連外。
- 時間：2026-06-19 05:01

## 測試需覆蓋 `_proc` 保存、`stop()` 終止與等待、逾時升級 kill、重複 stop 冪等、finally 不誤清新 proc、取消時清理回收、並發 speak 明確失敗。
- 時間：2026-06-19 05:01

## 稽核 log 落在 `auth.set_password()`，且只在 `.env`、`os.environ`、`config.ACCESS_PASSWORD` 全部更新成功後發一筆 `WARNING`。
- 時間：2026-06-19 05:57
- 理由：讓 log 代表實際成功狀態，並涵蓋 API 以外的未來呼叫路徑。
- 否決方案：不在 route 層補 log，避免呼叫路徑分散後漏記。

## 使用 auth 模組 logger `ti.auth`，不新增 audit subsystem。
- 時間：2026-06-19 05:57
- 理由：本輪只有單一安全事件，抽象成本高於收益。

## log 只記錄狀態，不記錄任何舊密碼、新密碼、hash、token、cookie 或可還原敏感資訊。
- 時間：2026-06-19 05:57
- 理由：稽核需要可觀測性，不需要秘密值。

## 非空密碼記錄「門禁已啟用／密碼已變更」語意；空字串記錄「門禁已停用／密碼已清空」語意。
- 時間：2026-06-19 05:57
- 理由：稽核者能分辨一般變更與停用認證。

## 失敗路徑不得發成功稽核 log；測試需覆蓋 `write_secret_file` 失敗時沒有 warning。
- 時間：2026-06-19 05:57
- 理由：防止未來重構把 log 移到寫入前。

## 本輪不修改 `/api/auth/password` 的長度限制；空字串停用只保留 `auth.set_password("")` 直呼能力。
- 時間：2026-06-19 05:57
- 否決方案：不把 API/UI 停用門禁混入本需求。

## 測試放在 `tests/server/test_auth.py`，沿用 `pw_env` 隔離環境，使用 `caplog` 驗證 `ti.auth` 的 warning。
- 時間：2026-06-19 05:57
- 理由：貼合現有測試邊界，不新增依賴。

## 測試覆蓋非空密碼、空字串停用、失敗不記錄三條路徑，並驗證實際舊密碼與新密碼值不出現在 log。
- 時間：2026-06-19 05:57

## 在 `studio/adr.py` 新增私有 `_MAX_STORE = 500` 作為 `adr.json` 持久化上限。
- 時間：2026-06-19 06:25
- 理由：對齊 `lessons.py` 模式，並切開儲存容量與 prompt 注入筆數。
- 否決方案：不使用 `config.ADR_MAX` 控制落盤容量。

## `adr.record()` 在鎖內完成讀取、既有保留資料去重、追加、裁切、`tmp.replace` 落盤。
- 時間：2026-06-19 06:25
- 理由：讓落盤狀態本身有上限，避免讀取時補救造成不一致。
- 否決方案：不引入新儲存層或外部檔案鎖依賴。

## 裁切策略為 `entries[-_MAX_STORE:]`，保留最新 500 筆且維持舊到新順序。
- 時間：2026-06-19 06:25
- 理由：行為簡單、可預測，符合目前 JSON 索引用途。

## 去重範圍只保證目前 `adr.json` 內保留的 500 筆。
- 時間：2026-06-19 06:25
- 理由：接受機讀索引有限容量後的有限去重。
- 否決方案：不為了全歷史去重反掃 `DECISIONS.md` 或建立額外索引。

## 重提仍在保留範圍內的舊 ADR 時，不刷新其位置；同批新增後若被裁掉即消失於 `adr.json`。
- 時間：2026-06-19 06:25
- 理由：維持既有「完全去重、不刷新」語意，避免暗中改變排序模型。

## 已被裁掉的很舊 ADR 未來若重提，可重新進入 `adr.json`，且 `DECISIONS.md` 會再次 append。
- 時間：2026-06-19 06:25
- 理由：這是容量治理的明確代價，換取實作簡單與依賴方向乾淨。

## 不裁切 `DECISIONS.md`。
- 時間：2026-06-19 06:25
- 理由：它是人讀審計歷史，保留完整追溯價值。
- 否決方案：不讓 UI 或 prompt 直接掃整份 `DECISIONS.md`。

## `context()` 繼續只用呼叫端 `limit` / `config.ADR_MAX` 控制注入筆數。
- 時間：2026-06-19 06:25
- 理由：`context` 是讀取展示策略，不承擔持久化容量治理。

## `record()` 的 `session_id`、`created_at`、回傳 `len(added)` 等 API 語意不變。
- 時間：2026-06-19 06:25
- 理由：本輪只處理儲存容量，不擴大行為面。

## 測試放在 `tests/core/test_adr.py`，用 `monkeypatch` 將 `_MAX_STORE` 降為 3，覆蓋最新保留、`context()` 不含裁掉資料、`DECISIONS.md` 保留完整 append。
- 時間：2026-06-19 06:25

## 補測重提最舊既有 ADR 並同批新增時，不刷新位置且可能被裁掉的行為。
- 時間：2026-06-19 06:25
- 理由：釘住刻意取捨，避免未來重構誤改語意。

## `DiscussionEngine.__init__` 新增 `own_history_recent_n: int | None = 3`。
- 時間：2026-06-19 06:47
- 理由：預設限制自身歷史最近 3 筆，降低 prompt 膨脹。
- 否決方案：不新增 env/config，避免擴大設定面。

## `own_history_recent_n=0` 代表完全不注入「你先前的發言」。
- 時間：2026-06-19 06:47
- 理由：必須特判，避免 Python `[-0:]` 變成全量。

## `own_history_recent_n=None` 代表不截斷、全量注入。
- 時間：2026-06-19 06:47
- 理由：保留除錯與相容逃生口。

## `own_history_recent_n < 0` 在建構時直接 `ValueError`。
- 時間：2026-06-19 06:47
- 理由：負數 slicing 語意隱晦，應明確拒絕。

## `None` 判斷必須先於 `< 0` 檢查。
- 時間：2026-06-19 06:47
- 理由：避免 `None < 0` 型別錯誤。

## 截斷邏輯集中在 `_build_prompt` 前的一小段前處理，不散落到 prompt 組字串流程。
- 時間：2026-06-19 06:47
- 理由：保持單一資料流，降低維護成本。

## `parallel` 與 `round_robin` 都沿用 `_build_prompt` 單一入口，不各自實作歷史截斷。
- 時間：2026-06-19 06:47
- 理由：保護依賴方向，避免分支行為漂移。

## `run()` 繼續完整累積 `own[name]`、`transcript` 與 summary 所需資料，只限制 prompt 注入。
- 時間：2026-06-19 06:47
- 理由：本輪只控 prompt 成本，不改流程語意。

## 最近 N 筆輸出仍維持原時間順序，由舊到新。
- 時間：2026-06-19 06:47
- 理由：對齊既有 `memory.recent_n` 語意。

## 每段自身歷史仍保留既有 `SELF_SEGMENT_MAX_CHARS=1200`。
- 時間：2026-06-19 06:47
- 理由：本輪限制的是筆數，不是 token 或字元總量。
- 否決方案：不導入 token budget、LangChain、LlamaIndex。

## 同步更新 `studio/discussion.py` 檔頭的實際建構子簽名/驗收文件。
- 時間：2026-06-19 06:47
- 理由：該段會被驗收抽查，漏改會造成低價值回歸。

## 測試放在 `tests/core/test_discussion.py`，覆蓋預設最近 3 筆、`0` 不注入、`None` 不截斷、順序不反轉、`-1` 觸發 `ValueError`。
- 時間：2026-06-19 06:47
- 理由：直接鎖住這次新增的 API 語意與主要陷阱。

## 本輪只處理 `studio/auth.py::set_password()` 稽核與 Ruff I001，不觸碰 discussion/prompt 截斷流程。
- 時間：2026-06-19 08:28

## 稽核點集中在 `set_password()`，維持密碼門禁狀態變更的單一入口。
- 時間：2026-06-19 08:28
- 理由：保護依賴方向，route 層不承擔 auth 內部狀態細節。
- 否決方案：在 route、middleware 或多處呼叫點分散記錄。

## 沿用既有 `ti.auth` logger 與標準庫 `logging`，成功事件用 WARNING。
- 時間：2026-06-19 08:28
- 否決方案：導入 loguru、structlog 或完整 audit subsystem。

## 資料流固定為寫入 `.env` 成功後，才更新 `os.environ`、`config.ACCESS_PASSWORD` 並發稽核 log。
- 時間：2026-06-19 08:28
- 理由：避免「記錄成功但實際失敗」的假 audit。

## log 只記「門禁已啟用」或「門禁已停用」。
- 時間：2026-06-19 08:28
- 否決方案：記錄密碼、hash、token、cookie、session 或差異內容。

## 停用時維持 `TI_ACCESS_PASSWORD=""`，不移除 env key。
- 時間：2026-06-19 08:28
- 理由：對齊既有 `bool("") == False` 語意。

## `set_password()` 的 `.strip()` 既有語意維持，不支援前後空白作為密碼內容。
- 時間：2026-06-19 08:28

## `write_secret_file` 失敗時自然拋例外，不更新 runtime state，也不發成功稽核 log。
- 時間：2026-06-19 08:28

## 測試放在 `tests/server/test_auth.py`，用 `caplog` 覆蓋啟用、停用、失敗不誤記與秘密不外洩。
- 時間：2026-06-19 08:28

## Ruff I001 只依既有 Ruff/isort 規則修 `tests/core/test_providers.py` import 排序。
- 時間：2026-06-19 08:28
- 否決方案：新增格式工具或手寫自訂排序規則。

## 本輪定義為輕量 audit；未來若要 actor/IP/request id，另建 audit event/context 層。
- 時間：2026-06-19 08:28

## 技術選型 - 沿用 `_rlimit_preexec` 作為 `preexec_fn` 套用資源限制，並於 `runner.py:L181` 明確註記「僅在單執行緒下安全，未來若引入多執行緒須翻案改為 prlimit」之警語。
- 時間：2026-06-19 21:33
- 理由：在主進程為單執行緒 `asyncio` 事件循環的前提下，此做法最為輕量且免除額外包裹腳本的複雜度；加註警語能確保此決策具備高可逆性，保護未來架構變更。
- 否決方案：否決直接重構為 `prlimit` 命令包裹器的過度設計，也否決不加警語、鎖死未來多執行緒擴充可能性的短視做法。

## 模組切分 - 資源上限限制邏輯完全內聚於 `runner.run_command_exec` 內部之沙箱與非沙箱分支中，保持呼叫端的介面簽名完全一致。
- 時間：2026-06-19 21:33
- 理由：隱藏實作細節，避免資源限制的具體參數洩漏至 `autopilot.py` 等呼叫端，保護依賴方向。
- 否決方案：否決在呼叫端暴露資源上限參數的設計，防止模組邊界劣化。

## 測試適應 - 於 `tests/conftest.py` 使用 `config.SANDBOX_BWRAP` 進行 `bwrap` 權限之動態探測，若無權限則 patch `_sandbox_available` 為 `False`，並使用 `warnings.warn` 拋出顯式警告。
- 時間：2026-06-19 21:33
- 理由：避免硬編碼 `/usr/bin/bwrap` 導致自訂路徑環境誤判，且透過 warning 機制防止 CI 環境中安全防線被「靜音」而產生假綠現象。
- 否決方案：否決硬編碼測試路徑以及不帶警告的靜默 patch 做法。

## 測試防線 - 於 `tests/core/test_rlimits.py` 移除「exec 不套資源上限」舊斷言，使用 mock 驗證沙箱啟用下 `preexec_fn` 的傳遞，並於非沙箱路徑實際斷言 `RLIMIT_AS` 行為。
- 時間：2026-06-19 21:33
- 理由：避免在無 PID namespace 權限的環境強跑 `bwrap` 導致 CI 紅燈，以 mock 傳參來兼顧安全性驗證與測試穩定性。
- 否決方案：否決在測試中強行執行實體沙箱的策略。

## `test_no_py_changed` 不列入本輪驗收範圍，護欄本體零修改。
- 時間：2026-06-20 19:12
- 理由：`tests/test_task1_retry_doc.py:160-169` 同時給出 docstring 與 skip 依據：docstring 明定「此護欄專屬 task#1 自身的 doc-only lane」，且 skip 條件僅對 `lane.startswith("task-") and lane != "task-1"` 生效；當前 cwd 不符 `task-*` lane 命名，新增任何 `.py` 後跑全套 pytest 必機械性紅，與本輪改碼性質無關。
- 否決方案：修改 skip 條件讓本輪 cwd 也能繞過——改題護欄即破壞 task#1 lane 的驗收語義，不可接受。

## 本輪唯一新增檔案為 `tests/test_scope_fixture_demo.py`，不新增任何其他 `.py` 或實體測試資料檔。
- 時間：2026-06-20 19:12
- 理由：最小可動方案；不引入多餘抽象層，不讓未來 maintainer 誤以為已建立完整 fixture 框架。
- 否決方案：另建 `tests/demo/` 子目錄——零收益多一層，不值得。

## 示範 fixture 寫在 `tests/test_scope_fixture_demo.py` 同檔，不放入任何 `conftest.py`。
- 時間：2026-06-20 19:12
- 理由：單一檔案使用，無需跨檔共用；放同檔 blast radius 最小，隔離最乾淨。
- 否決方案：放入根層 `tests/conftest.py`——該檔已承載 dotenv stub、env 清洗、autouse reset、bwrap 探測等全域副作用，不適合摻入子系統示範 fixture，會讓副作用範圍擴散到整棵測試樹。

## Fixture 升層規則——同子系統 2+ 檔共用時，才移入該子系統的 `conftest.py`（非根層）；根層 `tests/conftest.py` 本輪不動。
- 時間：2026-06-20 19:12

## Fixture 實作工具限定為 `tmp_path`（臨時目錄，測試後自動回收）與 `monkeypatch`（env/attr 測試後自動還原），禁止對真實 git worktree 操作或在 `tests/` 寫入實體資料檔。
- 時間：2026-06-20 19:12
- 理由：對真實 worktree 操作會影響 `test_no_py_changed` 的 `git diff` 基準；寫入 `tests/` 資料檔會製造 untracked 殘留，導致 `pre-commit --all-files` 與 CI 直接掃描結果分歧（CLAUDE.md 既有鐵則）。
- 否決方案：用真實 repo 子目錄存放 fixture 資料——製造跨測試執行順序依賴，且產生 untracked 殘留。

## 驗收命令以目標路徑收斂，不以 `-k "not test_no_py_changed"` 作為主要手段。
- 時間：2026-06-20 19:12
- 理由：目標路徑是隔離，`-k` 排除是掩蓋；用 `-k` 掩蓋只是讓全套命令表面綠，未來有人直接跑 `pytest` 仍踩雷，不保護依賴方向。
- 否決方案：在 CI 或 Makefile 裡加 `-k "not test_no_py_changed"` 全域排除——讓護欄靜默失效，破壞 task#1 lane 的長期防線。

## 正式驗收命令為 `python3 -m pytest tests/test_scope_fixture_demo.py -q && test -z "$(git diff -- tests/test_task1_retry_doc.py)" && echo GUARD_UNTOUCHED`，QA 以此為唯一可重跑基準。
- 時間：2026-06-20 19:12

## PR／驗收說明須明文標註「本輪僅驗 `tests/test_scope_fixture_demo.py`，不代表全套 pytest 已恢復；全套長期解法待 harness 以任務自身 baseline 比對後另行處理」。
- 時間：2026-06-20 19:12
- 理由：防止後續 reviewer 誤讀為全套已通過，保護依賴方向的透明度。
## 本輪零程式碼改動，唯一實作工作為補 `CLAUDE.md`（或獨立 `docs/release-ops.md`）段落，納入 GH_PAT 設定指引與半閉環聲明。
- 時間：2026-06-21 00:53
- 理由：現有 workflow、script、守護測試已閉合需求；額外動碼只增 blast radius，不增保障。
- 否決方案：補 `--verify-tag`、換 GitHub App token——兩者均屬鍍金，本輪需求未要求且守護測試已覆蓋等效保護。

## `GH_PAT` 文件必須逐字列出四項規格——① fine-grained PAT、② 本 repo only（非 all-repos）、③ 權限 `Contents: read/write`、④ secret 名稱固定 `GH_PAT`。
- 時間：2026-06-21 00:53
- 理由：secret 名稱或範圍錯誤是最常見的「設定偏移」，文件若不逐字釘住，後繼維護者輪替 PAT 時容易產出不相容的 token，且 Step 5 失敗僅回 403，錯誤訊息無法自述原因。
- 否決方案：只寫「設定一個 PAT」——資訊不完整，保護不了依賴方向。

## GH_PAT 到期 / 輪替責任須在同一文件段落明文標注「過期 → Step 5 `gh release create` 403；輪替後到 repo Settings → Secrets 更新 `GH_PAT`」，不得省略。
- 時間：2026-06-21 00:53
- 理由：高級工程師點出「過期時 Step 5 會 403」是最大長期運維風險；把處置流程寫進文件是最低成本的防護。

## 「半閉環」聲明放入 `CLAUDE.md`，措辭固定為「真實 `v*` tag-push 端到端尚待生產驗證，單元/守護測試為半閉環」，不得以模糊字眼替代。
- 時間：2026-06-21 00:53
- 理由：workflow 註解僅對讀原始碼的人可見；`CLAUDE.md` 是本 repo 的正式協作記憶，後續 reviewer 必然讀到，保護依賴方向的透明度成本最低。
- 否決方案：只藏在 workflow 註解——可見性不足，reviewer 可能誤判已有完整 E2E 驗證。

## `--verify-tag` 本輪不加；若未來引入 `workflow_dispatch` 手動觸發，此決策必須重審並記入新 ADR。
- 時間：2026-06-21 00:53
- 理由：現有觸發條件 `on: push: tags: v*` 保證 tag 已存在，Step 3 `ref_name == v{version}` 為等效 fail-fast；重複加只鍍金。

## Token 路由維持 `secrets.GH_PAT`（fine-grained PAT），不引入 GitHub App。
- 時間：2026-06-21 00:53
- 理由：PAT 已解 GITHUB_TOKEN 遞迴死結，範圍可限縮到本 repo；App 需額外安裝與密鑰管理，引入新設定依賴，可逆性下降。
- 否決方案：GitHub App token——成本高於收益，本輪需求未要求審計追蹤粒度。

## 模組邊界維持 `scripts → studio` 單向依賴；`studio/release_note.py` 永遠不感知 `body.md` 路徑或 `GITHUB_OUTPUT`，I/O orchestration 集中在 `scripts/publish_release.py`。
- 時間：2026-06-21 00:53

## version 與 heading 字面值禁止出現在 YAML 與腳本內；`test_script_has_zero_breaking_heading_literal` 守護測試為此不變量的機械護欄，本輪及後續均不可碰。
- 時間：2026-06-21 00:53

## 正式驗收命令為 `python3 -m pytest tests/autopilot/test_qa_task2_release_body.py tests/autopilot/test_qa_task3_release_trigger_chain.py tests/autopilot/test_qa_task4_publish_workflow_guard.py tests/autopilot/test_release_pipeline_dry_run.py -q && python3 scripts/publish_release.py && echo BODY_RENDERED`，QA 以此為唯一可重跑基準；文件須明文標注「本輪守護測試為半閉環，不代表真實 tag-push E2E 已驗證」。
- 時間：2026-06-21 00:53
## **Patch 接縫統一用 `sys.modules["openai"]`，禁用 `studio.providers.openai`**
- 時間：2026-06-21 09:36
- 理由：`_openai_chat` 是 lazy import，patch alias 會是假綠；此規則強制寫入各新測試 docstring，`monkeypatch` 自動還原
- 否決方案：patch `studio.providers.openai`——lazy import 路徑下對應到錯誤接縫，測試會恆通過

## **任務 #2 新增獨立檔案 `tests/core/test_providers_max_retries_gemini_task2_qa.py`，不擴充既有 openai/minimax 檔案**
- 時間：2026-06-21 09:36
- 理由：單一 provider 失敗時可精確定位；blast radius 隔離；與既有 openai/minimax 各自獨立的慣例一致
- 否決方案：三 provider 同一檔——對稱性好看，但任一 provider 新需求會汙染其他 provider 的穩定測試

## **任務 #3 錯誤分類測試採「注入 `expert._chat` callable」，fake exception 物件須符合 `llm_caller.py` 實際分類型別，不得只丟泛用 `Exception`**
- 時間：2026-06-21 09:36
- 理由：`run_with_retries` 依例外類型分流（429/503/401 等），丟錯型別會跑到錯的分支，測試喪失判別力
- 否決方案：mock SDK class——比注入 callable 多一層間接，且與 lazy import 接縫不符

## **任務 #3 每個錯誤 case 須有反向黑樣本，最低要求：`max_retries=0` 情況下斷言 `fake_fn` 只被呼叫一次**
- 時間：2026-06-21 09:36
- 理由：防止 retry 測試假綠——若斷言只看「最終未拋出」而不看呼叫次數，retry 邏輯壞掉也會通過

## **任務 #4 範圍重定：本輪測試「工具解析失敗回退 content」＋「驗證現有 `DedupCache/execute_deduped` 機制覆蓋 OpenAI 相容路徑（gemini/minimax）」，新增檔案 `tests/core/test_providers_compat_tool_behavior_task4_qa.py`**
- 時間：2026-06-21 09:36
- 理由：`DedupCache`（providers.py:847）與 `execute_deduped`（providers.py:896）在 production 已存在，且 `test_providers_dedup_task3.py:77` 已有 e2e 覆蓋；本輪缺口是「OpenAI 相容後端是否確實走同一去重路徑」，而非「機制不存在」
- 否決方案：以「production 無 idempotency 機制」為由排除非冪等測試——與現況不符，會誤導 PR reviewer 和後續維護者

## **任務 #4「工具解析失敗回退 content」須含反向黑樣本：格式正確的 tool_call 應觸發工具迴圈，不被誤當 content 吞掉**
- 時間：2026-06-21 09:36

## **移交待辦（明文記入 PR）改寫為：「確認 `test_providers_dedup_task3.py` 現有覆蓋是否已涵蓋 gemini/minimax 相容後端的去重語意；若有路徑缺口，另開任務補特定 provider 的 `DedupCache` 行為驗證」**
- 時間：2026-06-21 09:36
- 理由：不能以「機制不存在」當理由——機制已在；真實移交項是「相容路徑的覆蓋完整度確認」
- 否決方案：待辦寫「非冪等去重機制設計」——暗示 production 待開發，與現況矛盾

## **三支新測試檔案統一放 `tests/core/`，命名遵循 `test_*_taskN_qa.py` 慣例，fixture 寫同檔不動 `conftest.py`**
- 時間：2026-06-21 09:36

## **驗收命令為 `python3 -m pytest tests/core/ -q`，基線 765 passed，新增後數量增加且無迴歸；不以 `-k` 排除作主要手段**
- 時間：2026-06-21 09:36
## `live` fixture 第 109 行改 `env["TI_ACCESS_PASSWORD"] = ""`，取代現有 `env.pop("TI_ACCESS_PASSWORD", None)`
- 時間：2026-06-21 03:15
- 理由：`pop` 後鍵不存在，子行程 import `config.py` 時 `load_dotenv(override=False)` 從 `.env` 補回密碼，門禁重啟；設空字串讓鍵存在於 subprocess env，dotenv 不覆寫，`auth_enabled()` 返回 False，門禁確實停用
- 否決方案：維持 `pop` 並搭配 `.env` 備份清除密碼——侵入真實 `.env` 結構、teardown 順序脆弱，不可接受

## 門禁停用後走 `require_loopback` 路徑，測試請求來自 127.0.0.1，本機檢查可通過，不需任何登入 cookie
- 時間：2026-06-21 03:15
- 理由：本測試目的是驗 persistence，不是驗 auth；auth 流程由專屬 test suite 覆蓋

## 不走 `/api/login` 取 cookie 再帶入 `_post`
- 時間：2026-06-21 03:15
- 理由：本測試不該混入 auth 流程，測試意圖單一才可維護；`AUTH_SECRET` 只是輔助理由，真正主因是職責分離
- 否決方案：用 `httpx.Client` 走真實 login 取 cookie——雖技術可行（同一 server process 的 cookie 有效），但把 persistence 測試與 auth 測試耦合，維護成本不值

## 不引入 `httpx`，`_get`／`_post` 維持現有 `urllib` 實作
- 時間：2026-06-21 03:15
- 理由：門禁停用後 `_post` 無需 cookie，現有實作足夠，零依賴新增

## `require_admin` / `WRITE_DEPS` 產品碼不動，`POST /api/settings` 維持受保護
- 時間：2026-06-21 03:15
- 理由：安全邊界不因測試退讓；測試應固定環境狀態以符合端點要求，而非降低端點要求以符合測試

## `/api/health` 保持公開 GET，`test_live_reload_effect_via_health` 的 reload 佐證繼續走 health 的 `provider` 欄位，不改端點設計
- 時間：2026-06-21 03:15
- 理由：health 本就公開，此路徑正確，只需修 fixture 認證狀態

## 本輪唯一變更落點為 `tests/test_qa_task4_persistence.py` 單一行，不新增檔案，不觸碰護欄檔
- 時間：2026-06-21 03:15
- 理由：blast radius 最小，diff 可自證

## 驗收命令為 `python3 -m pytest tests/test_qa_task4_persistence.py -q && git status --porcelain`，diff 必須只差 fixture 那一行
- 時間：2026-06-21 03:15

## 後續統一慣例——凡 subprocess fixture 需停用門禁，一律用 `env["TI_ACCESS_PASSWORD"] = ""`，禁止 `pop`；本輪不統一修，列為跟進待辦
- 時間：2026-06-21 03:15
- 理由：高級工程師指出 repo 內多處同類 `pop` 有同樣漂移風險；本輪先固定 task4，範圍不擴大，但慣例須明文記錄避免新 fixture 複製舊錯誤

## `_derive_scorecard`（`history.py`）新增四個欄位：`qa_total`、`qa_pass`、`critic_total`、`critic_pass`，從事件流確定性推導，原有欄位（rejects、demo_passed 等）原樣保留。
- 時間：2026-06-28 18:24
- 理由：保留分子分母而非直接算單場率，讓聚合層能 sum 後除，避免平均的平均造成小樣本扭曲。
- 否決方案：單場直接算通過率再存入 scorecard——聚合層無法重新加權，小場次（1 次 QA）會放大失真。

## `run_result` 計 QA 計數時，改為「所有非自測 run_result 都計 qa_total，`passed is True`（嚴格型別比對）計 qa_pass」，與現有 fail-only 退回邏輯獨立分開，不混用同一分支。
- 時間：2026-06-28 18:24
- 理由：現有 reject 路徑只走 failed；若複用同一 `elif not passed` 分支，pass 事件的分母永遠漏計。
- 否決方案：在現有失敗分支補計數——會導致 qa_total 只記失敗場次，分母語意錯誤。

## `critic_review` 計數：所有 `critic_review` 事件計 `critic_total`，`passed is True` 計 `critic_pass`，無需特別條件篩選。
- 時間：2026-06-28 18:24

## `demo_passed` 在 `_derive_scorecard` 維持現有 `bool | None`，不新增欄位。Demo 通過率由聚合層以場次維度計算。
- 時間：2026-06-28 18:24
- 理由：一場最多一個 demo_result，場次維度與事件維度等價，單場不需要分子分母結構。

## `_aggregate_scorecard`（`routes.py`）新增三個跨場通過率：`qa_pass_rate`、`critic_pass_rate`、`demo_pass_rate`，分母為 0 時一律回傳 `None`，不回傳 0。
- 時間：2026-06-28 18:24
- 理由：`0` 會被前端與使用者誤讀為「通過率為零」，`None` 語意才是「本輪無資料」，對應前端顯示 `—`。
- 否決方案：分母為 0 時回傳 0——語意歧義，無法區分「所有測試都失敗」與「根本沒跑過測試」。

## 聚合時舊 meta.json 缺新欄位一律用 `.get("qa_total", 0)` 等防守讀取，自動視為 0，不丟例外。Demo 通過率以 `demo_passed is not None` 判定該場是否有 Demo。
- 時間：2026-06-28 18:24

## `web/app.js` 在 `sc.n > 0` 區塊的現有 rows 內，以 `rows.push` 新增「測試通過率」「Demo 通過率」「審查通過率」三行，格式沿用既有 `pct()` 函式，不改 DOM 結構。
- 時間：2026-06-28 18:24
- 否決方案：改 DOM 結構或新增渲染函式——diff 更大、回歸風險高、三行 push 已足夠。

## 測試（`tests/server/test_scorecard.py`）至少補四個案例：自測排除不計入 qa_total、QA 加總後除（非平均的平均）、critic 加總後除、舊 scorecard 缺新欄位仍可聚合且三率回傳 None；另補 demo 聚合含 None（無 demo 場次）邊界案例。
- 時間：2026-06-28 18:24

## 模組邊界不動——計數推導在 `history.py`，聚合在 `routes.py`，前端在 `web/app.js`；不觸碰 `flow.py`、`events.py`、`orchestrator.py`，不新增依賴，不動 marker 字串。
- 時間：2026-06-28 18:24

## `classify_failure_followups(failed_titles: list[str], retro_items: list[dict]) -> list[dict]` 放 `flow.py`，為無狀態純函式。
- 時間：2026-06-28 19:15
- 理由：可單元測試、可 monkeypatch；決策解析屬 `flow.py` 職責。

## 失敗標題一律輸出為 `priority=0, type="bug"`，**不論其是否出現在 retro_items、也不論 retro_items 裡該標題的現有 priority**——對命中的失敗標題做 upsert（覆寫），而非 skip。
- 時間：2026-06-28 19:15
- 理由：`parse_followups_meta` 無法區分「PM 明確標 P1」與「PM 省略標籤被補預設 P1」，若保留 retro 值，失敗標題不帶標籤時根本問題不解決。機器確認的失敗是客觀事實，比 LLM 輸出的預設值更可信。
- 否決方案：「PM 標注優先不覆蓋」——此策略在 `tag_explicit` 欄位不存在的前提下有 correctness 漏洞；若未來要支援 PM override，先補 `tag_explicit: bool` 到 parser，再談。

## retro_items 中**未命中失敗標題**的項目原樣保留（title/priority/type 不動），累加到回傳結果；失敗標題命中 retro_items 則就地升格（upsert）、不產生重複項。
- 時間：2026-06-28 19:15

## `_wrap_up` 簽名改為 `_wrap_up(pm, all_ok, demo_veto=False)`，明確傳入 `demo_veto`；呼叫端（行 1321）同步補參數，不從 `all_ok=False` 推斷。
- 時間：2026-06-28 19:15
- 否決方案：從 `all_ok` 反推 `demo_veto`——`all_ok` 是多源合併布林，無法可靠還原 demo 失敗語意。

## `demo_veto=True` 時注入固定標題 `"修復 Demo 失敗"` 至 `failed_titles`（priority=0/bug），格式固定不帶摘要。
- 時間：2026-06-28 19:15
- 理由：摘要含 LLM 非確定性文字，會讓精確比對去重失效。

## `_record_known_limitations` **只替換** `_followup_items.append({"title": t, "priority": 1, "type": "improvement"})` 這段硬編碼，調用 `classify_failure_followups` 取代之；`KNOWN_LIMITATIONS.md` 寫檔與 broadcast 副作用**完整保留**，不移動、不刪除。
- 時間：2026-06-28 19:15
- 否決方案：整段替換 `_record_known_limitations`——工程師已確認它同時負責寫文件，整段刪除會移除非 followup 的副作用。

## `orchestrator.py` 頭部 re-export：`from .flow import classify_failure_followups as classify_failure_followups`，與現有 `parse_followups_meta` re-export 同模式。
- 時間：2026-06-28 19:15

## 測試必含以下四個明確案例：① 失敗標題不在 retro → 回傳 P0/bug；② 失敗標題在 retro 且**無標籤**（預設 P1）→ 仍升格為 P0/bug（此為高工點名的根因路徑）；③ 同一 backlog 內 P0 失敗待辦經 `backlog.next_pending` 排在 P1 retro 建議前；④ 空 `failed_titles`、空 retro_items 各自不崩潰。
- 時間：2026-06-28 19:15
- 理由：案例②是最容易漏掉且最貼近根因的路徑，必須顯式守住。

## 整鏈測試用真實 `backlog.add_items` + `backlog.next_pending`，不 mock；確保 P0 排序這條鏈不依賴 mock 假設。
- 時間：2026-06-28 19:15

## 本輪不引入 `tag_explicit` 欄位到 `parse_followups_meta`——範圍屬 PM override 精細化，留待獨立任務處理，避免本輪範圍擴散。
- 時間：2026-06-28 19:15

## `_commit_push_merge` 頂端 guard 使用 `config.AUTOPILOT_REPO`（config 值），**不**使用 `publisher.current_repo()`
- 時間：2026-06-28 20:39
- 理由：函式內 `gh pr create -R repo` 第 302 行已直接用 `config.AUTOPILOT_REPO`，`publisher.current_repo()` 只在 `_merge_flow` 段才有意義；頂端 override 尚未設定前讀 `current_repo()` 一定是錯的。
- 否決方案：「頂端讀 `publisher.current_repo()` 判斷」——override 不存在時讀到 `PUBLISH_REPO` 或空字串，正常路徑被誤擋。

## Guard 條件二選一觸發 `return (False, reason)`：① `not config.AUTOPILOT_REPO`（設定空）；② `config.PUBLISH_REPO` 非空 **且** 正規化後 `config.PUBLISH_REPO != config.AUTOPILOT_REPO`
- 時間：2026-06-28 21:12
- 理由：autopilot 推送路徑只允許空 `PUBLISH_REPO` 或與 `AUTOPILOT_REPO` 指向同一 repo；若 `PUBLISH_REPO` 指向另一個專案 repo，直接拒絕，避免核心自改流污染專案 repo。比較採不分大小寫 repo key。

## `_commit_push_merge` 在真正 `git push` 前必須確認 `git remote get-url --push origin` 正規化後等於 `config.AUTOPILOT_REPO`
- 時間：2026-06-28 21:12
- 理由：入口 guard 只能證明 config 目標正確，不能證明傳入 clone 的 `origin` 沒被改；push 前檢查實際 push URL 才能把「實際推送目標 == AUTOPILOT_REPO」變成執行期合約。

## guard check **之後**立即執行 `token = publisher.set_repo_override(config.AUTOPILOT_REPO)`，並以 `try/finally: publisher.reset_repo_override(token)` 包住函式剩餘全部 body
- 時間：2026-06-28 20:39
- 理由：工程師正確指出 override 必須早於任何可能呼叫 `publisher.current_repo()` 的後段程式碼（含未來新增段落）；`try/finally` 確保異常時也還原狀態。
- 否決方案：「只包 `_merge_flow` 段」（現況）——未來有人在 PR 建立後、merge 前加入任何 `publisher.current_repo()` 呼叫會無 override 保護。

## 刪除現有第 340-350 行的內層 `set_repo_override`/`reset_repo_override` 對——它被上移的外層 `try/finally` 完整涵蓋，留著會造成雙重 override 混淆
- 時間：2026-06-28 20:39

## 測試四案，各自獨立不共用 monkeypatch 狀態
- 時間：2026-06-28 20:39

## Case A（wiring 案，高工點名的核心）——**不** mock `publisher.current_repo`；mock `_run` 回傳成功、mock `publisher._merge_flow` 在呼叫時即時讀取 `publisher.current_repo()` 並記錄，事後斷言記錄值 == `config.AUTOPILOT_REPO`
- 時間：2026-06-28 20:39
- 理由：這是唯一能證明「真實 override wiring 有效」的測試；若改成 mock `current_repo` 則測不到覆寫鏈，高工說「把錯誤契約固定下來」。

## Case B——monkeypatch `config.AUTOPILOT_REPO = ""`，呼叫函式，斷言回傳 `(False, ...)` 且 `_run`/`_merge_flow` 從未被呼叫（無 push/PR 副作用）
- 時間：2026-06-28 20:39

## Case C——monkeypatch `config.PUBLISH_REPO` 指向不同專案 repo，呼叫函式，斷言回傳 `(False, ...)` 且無 push/PR 副作用
- 時間：2026-06-28 21:12

## Case D（邊界）——`config.PUBLISH_REPO = ""`（預設值），`config.AUTOPILOT_REPO` 正常，mock `_run`/`_merge_flow` 讓流程跑完，斷言回傳 `(True, ...)` 不被誤擋
- 時間：2026-06-28 20:39

## 測試檔路徑 `tests/autopilot/test_qa_no_publish_pollution.py`，四案皆為 `async def`（`asyncio_mode = "auto"`）；`_run`/`_merge_flow` 用 `AsyncMock`，不打真實 git/GitHub
- 時間：2026-06-28 20:39

## `CLAUDE.md` 新增「安全自改合約：`_commit_push_merge` 不變式」節，說明：guard 條件位置、`try/finally` 覆蓋範圍；並明列本輪排除（audit.jsonl、`AUTOPILOT_DAILY_PR_BUDGET`）為移交待辦
- 時間：2026-06-28 20:39

## 不新增依賴、不改 `config.py`/`publisher.py` 任何公開介面；`_REPO_OVERRIDE` / `set_repo_override` / `reset_repo_override` 契約完整保留
- 時間：2026-06-28 20:39
## 本輪零改動原則——只做執行驗證，不寫新碼、不改介面、不補文件。
- 時間：2026-07-04 01:32
- 理由：基線已確認問題不存在；任何新改動都會讓「已完成」重新變成「待 CI 驗證」，成本高於收益。
- 否決方案：趁機補測試覆蓋或 `secure_write.py` 文件——另開任務，不混本輪。

## pytest 執行一律加 `--cache-dir=$TMPDIR/pytest-cache-closure`，防止 `.pytest_cache` 落在工作樹污染 `git status`。
- 時間：2026-07-04 01:32
- 理由：工程師指出 pytest 可能產生 cache/coverage 暫存檔；若不重導，零改動閉環無法自證乾淨。
- 否決方案：事後 `git clean` 清 cache——破壞性操作，且可能掃掉非 cache 殘留，不可預測。

## `git status --short` 驗收定義為「pytest 執行後輸出仍為空」，而非「目錄絕對無任何殘留檔」。
- 時間：2026-07-04 01:32
- 理由：高工已確認當前 `git status --short` 乾淨；根目錄的 `.cmd_hits*.txt`、`CLOSURE_task*.md` 現已不影響 git 狀態（已追蹤或已 ignore）。工程師的衝突假設在此情境不成立。
- 否決方案：改成「除既知殘留物外無新增差異」——條件模糊，需人工判讀，不適合作為可重現的自動驗收門檻。

## 三任務可並行執行，但最後設單一彙整判定點——三者結果全部可見後，才輸出一份明確的「已完成 / 未完成」結論。
- 時間：2026-07-04 01:32
- 理由：高工與工程師皆指出「不設匯總 gate」容易漏看結果；並行省時，但閉環需要單一責任點。
- 否決方案：三任務各自結案、無彙整——容易出現「兩通一漏」卻誤判全綠的場景。

## `--collect-only` 判定以 pytest exit code（= 0）加 summary 行為主，尾部 `errors` 區段存在與否為輔助確認。
- 時間：2026-07-04 01:32
- 理由：exit code 是結構化機器判斷，不受 warning 噪音干擾；高工建議避免只靠肉眼 tail。
- 否決方案：只 grep `errors` 關鍵字——行內字樣誤判率高（過去教訓）。

## `collected ≥ 3386` 標記為本輪 baseline 魔術數字，結案文件須明注「僅為本輪基準，不作長期架構契約」。
- 時間：2026-07-04 01:32
- 理由：高工指出此數字會隨測試增減漂移；若不標注，下輪接手者可能誤用為硬性門檻。
- 否決方案：直接硬寫 3386 進 CI 驗收腳本——鎖死後每次新增測試都要人工更新，維護成本不對稱。

## 根目錄殘留物（`.cmd_hits*.txt`、`CLOSURE_task*.md`）列為移交待辦、獨立開票，本輪不觸碰。
- 時間：2026-07-04 01:32
- 理由：高工確認這些檔案當前不影響 git 狀態；若本輪清理反而引入 diff，破壞零改動自證。

## 技術選型——零新依賴；不引 OTel SDK、不綁 experimental semconv schema，僅在新增面（`task_result` payload、`_task_perf`、考核 objective）採 OTel 命名 `input_tokens`/`output_tokens`/`total_tokens`/`cost_usd`
- 時間：2026-07-04 09:01

## 既有 `token_usage` 事件的 `prompt_tokens`/`completion_tokens` 欄位名零改動，OTel 命名只用於新面
- 時間：2026-07-04 09:01
- 理由：硬改舊欄位會牽動 history 回放、report、既有測試三個相容面，收益只有命名一致；join 層做一次 mapping 成本極低
- 否決方案：本輪將 token_usage 事件欄位改名或加別名雙寫（雙寫會讓兩個鍵名並存成為永久債）

## task_id 注入沿用既有 `_tagged_broadcast`/`_speak` 鏈路，禁止新建平行包裝器；本輪新增僅三件：`events.token_usage` 選填 `task_id` 參數、`_counting_broadcast` 聚合分支、`task_result` 事件
- 時間：2026-07-04 09:01
- 理由：機制去重——兩個做同一件事的包裝器，半年後接手者分不清權威；既有 tagged（外）→ counting（內）層次天然正確
- 否決方案：原設計的「任務迴圈內新建 closure 包裝器」（與 `_tagged_broadcast` 功能重複）

## `_tagged_broadcast` 的 task_id 一律取自任務工作迴圈的 `task["id"]`，與 lane 身分脫鉤——循序模式（main lane）必須同樣標記，補「循序單 lane 也正確歸因」的黑白測試
- 時間：2026-07-04 09:01
- 理由：工程師查核發現 `_lane_tag()` 主 lane 回 None，若歸因綁 lane 身分，循序模式全漏算
- 否決方案：只有並行 lane 才標 task_id 的現狀語意

## 契約測試鎖 `setdefault` 語意（不覆蓋既有 task_id），並同時覆蓋「factory 傳入」與「wrapper setdefault 補入」兩條路徑產出相同鍵名
- 時間：2026-07-04 09:01
- 理由：巢狀包裝時外層不得改寫內層歸因；events.py 仍是鍵名 SSOT，但實務寫入多走 wrapper

## per-task 聚合放 `_counting_broadcast` 新增分支——讀 `p.get("task_id")` 累進 `_task_perf[task_id]`（input/output/total_tokens、cost_usd、cost_source），沿用既有「異常忽略、永不阻斷事件流」容錯；huddle 重試沿用 duration_s 的累加模式
- 時間：2026-07-04 09:01

## cost_source 契約定義三值枚舉 `reported`/`estimated`/`mixed`，但本輪只產出 `reported`（provider 實報 cost_usd 存在時）；cost 缺時 `cost_usd=None`、`cost_source=None`——估算器列移交待辦、獨立開票
- 時間：2026-07-04 09:01
- 理由：工程師指出 OpenAI-compatible 多半無 cost，「估算在哪裡補」未定義前，estimated 只是空承諾；先鎖契約形狀、不塞半成品邏輯，可逆
- 否決方案：本輪同步實作估算器（擴散到 providers 計價表，範圍爆炸）

## 新增事件型別 `task_result`（payload: task_id、role、provider、model、duration_s、qa_rounds、input/output/total_tokens、cost_usd、cost_source），於 `_collect_task_perf` 後廣播；`dispatch_decision` payload 與語意零改動，消費端以 task_id join
- 時間：2026-07-04 09:01
- 理由：決策快照與結果的時序語意乾淨（決策當下結果不存在）、舊 history 回放零風險
- 否決方案：把結果欄位塞回 dispatch_decision（污染既有契約、回放風險）

## 討論階段（澄清/架構/檢討）token 不歸因、task_id 維持 None，不發明偽任務 id；實作前必須確認任務範圍內所有 LLM 呼叫（含 huddle 走 `discussion.py` 自有 `_broadcast` 的路徑）是否都經 `_speak`——有例外即列為已知缺口，寫進測試註解與移交待辦，不默默漏
- 時間：2026-07-04 09:01
- 理由：高工指出 discussion.py 用自己的 broadcast，可能繞過 tagged 鏈路；缺口要顯式化

## 考核 objective 匯出（orchestrator.py:3230 附近）每任務併入 `total_tokens`/`cost_usd`/`cost_source`（缺資料＝None 不拋錯，沿用考核旁路永不 raise）；PM 考核提示中 qa_rounds 與 token 分開呈現、不重複懲罰
- 時間：2026-07-04 09:01

## 向後相容雙向守護——舊 jsonl（無 task_id）經 `_derive_token_usage`/`usage_report` 輸出 bit-for-bit 不變；另補「新格式事件（含 task_id 鍵）餵舊聚合」樣本證明無感
- 時間：2026-07-04 09:01
- 理由：相容是雙向的：舊資料進新碼、新資料進舊消費端，只測一向會漏

## 實作分三小步依序落地：①`events.token_usage(task_id=None)` + `_counting_broadcast` 聚合分支 → ②`task_result` 事件 + objective 併入 → ③前端 smoke 與文件；每步獨立可驗收
- 時間：2026-07-04 09:01
- 理由：工程師建議避免一次改爆相容面；與 PM 任務依賴序（#1→#2→#3）一致

## `flow.py` 本輪零改動（無新解析需求）；前端 `handleEvent()` 對 `task_result` 僅做「未知事件不崩潰」最小處理，UI 呈現不在本輪範圍
- 時間：2026-07-04 09:01

## 不新建模組，全部擴充 `studio/lessons.py`＋`orchestrator.py` 兩處接線；`flow.py`、`events.py` 零改動
- 時間：2026-07-04 16:35
- 理由：vote_result 事件與注入管線既存，這是接線題不是造輪題；零新依賴。
- 否決方案：引入外部記憶框架（Mem0 等）或新建 memory 模組——500 筆純文字規模無收益，違反不隨意加依賴。

## `add_many` 加 keyword 參數 `source: str = "retro"`（枚舉 `vote`/`appraisal`/`retro`），只落檔不參與挑選；`distill()` 手動重建 item 處同步補 `source`；舊資料無 source 鍵以 `.get()` 容錯，零遷移
- 時間：2026-07-04 16:35
- 理由：預設值使既有 3284 呼叫端零改動；工程師抓到 distill 重建 item 會漏欄位，一併補。

## 去重升級為雙模式——`add_many` 加 keyword 參數（如 `exact_only: bool = False`）：預設走「全文精確快速路徑＋`difflib.SequenceMatcher` 模糊比對」；vote 接線端傳精確模式，`表決先例:` 條目**只做全文精確比對、跳過模糊去重**
- 時間：2026-07-04 16:35
- 理由：高工指出的正確性漏洞——固定模板下同 topic 不同 winner 只差幾字，模糊比對會把新裁決默默擋掉、舊先例永遠勝出。用參數讓政策留在接線端，儲存層只提供機制，不做前綴嗅探。
- 否決方案：只靠閾值校準讓黑樣本通過——閾值是連續值賭注，模板前綴墊高相似度使安全區間過窄，機制性跳過才可靠。

## 模糊閾值由工程師以現有 lessons 資料實測校準，校準依據寫進註解；測試必含「同 topic 不同 winner 不得判重」黑樣本、「幾乎同文應判重」白樣本，以及 retro 路徑模糊判重的明確覆蓋（行為變更不得只是副作用）
- 時間：2026-07-04 16:35
- 理由：高工附帶約束 1、2 直接納入驗收。

## 品質閘門全放 orchestrator 接線端——vote：`tie` 或 `degraded` 不入庫；appraisal：僅 `score ≤ 2` 且 `comment.strip()` 非空入庫；lessons.py 不含任何來源特定判斷
- 時間：2026-07-04 16:35
- 理由：政策/儲存分離，閘門規則未來可改而不動儲存層。

## appraisal 接線復用 `_record_appraisals()` 已 parse 的 rows，不重 parse
- 時間：2026-07-04 16:35
- 理由：工程師建議，避免雙 parse 漂移。

## 模板固定為 `表決先例: <topic> → <winner>` 與 `考核教訓(<score>分): <comment>`，格式字串以測試斷言鎖住，比照 marker 字串慣例
- 時間：2026-07-04 16:35

## 本輪所有新條目 scope 維持 `global`；vote 先例可遷移性風險以品質閘門＋source 可追溯承擔
- 時間：2026-07-04 16:35
- 理由：注入點 1262 的 `context()` 不帶 scope，project scope 會斷閉環（驗收 #5）。
- 否決方案：vote 先例鎖 project scope——需同步改造注入點，超出本輪範圍。

## 兩處接線失敗僅 `logger.warning`，不 raise、不阻斷 broadcast 與收尾；入庫走同步呼叫，沿用既有 3284 retro 模式（檔小鎖短）
- 時間：2026-07-04 16:35

## 移交待辦四項另開票——①use_count 結合後續低分的降權淘汰；②500 筆稀釋時高價值教訓保留策略；③同 topic 矛盾先例的仲裁/覆蓋機制；④`distill` 蒸餾會抹除 `source` 且可能改寫/合併表決先例，「可追溯」非長期保證
- 時間：2026-07-04 16:35
- 理由：高工附帶約束 3——別讓後人把可追溯當成蒸餾後仍成立的承諾。

## autopilot 心跳新增 `workers` 子行程活性欄（issue #285）：以掃 `/proc/[0-9]*/stat` 建 ppid→children map 展開 os.getpid() 後裔子樹、取 utime+stime，跨兩次心跳 tick 比 delta 得 `cpu_active`
- 時間：2026-07-04
- 理由：長輪多專家討論的 inter-message 間隔（單一長工具呼叫/長 thinking/單則超長串流）期間無事件產出，`last_activity_at`(=events mtime) 凍結 30-90 分鐘被外部監控誤判死鎖並 restart（同日兩次、丟失數小時進度）。把人工診斷用的「對 claude 子行程做兩次 /proc utime/stime 取樣」自動化寫進 status.json，讓監控能肯定判定「有 worker 燒 CPU＝非死鎖」。
- 選型：掃 ppid map 而非 `/proc/<pid>/task/<tid>/children`——後者依賴內核 `CONFIG_PROC_CHILDREN` 且並發下不保證完整；ppid 是任何 /proc 恆有的欄位，可攜性最高，且同趟就地取 utime/stime 免二次讀檔。
- 不引 psutil：守「不隨意新增依賴」鐵則，純標準庫 `os` 讀 /proc 文字檔即可；delta 只比大小不換算秒，故不需 `SC_CLK_TCK`。
- None 三態語義：`_proc_descendant_cpu` 回 `dict`（`{}`＝明確零 worker）／`None`（/proc 不可用或解析失敗，絕不拋例外）；`workers.cpu_active` 另有首 tick=None（尚無前次快照可比）。監控見 `cpu_active == null` 時須退回 `last_activity_at` 判斷，不得單憑 null restart。
- 否決方案：改用 event-driven 細粒度心跳（每 broadcast 事件即刷）——無法解「專家單則訊息之間根本不產事件」的盲區，子行程 CPU 取樣才是與事件粒度解耦的存活證據。
- 移交待辦（本輪不含）：minimax.io CLOSE-WAIT / httpx 連線池洩漏（issue 建議 #3）——`studio/providers.py::_openai_chat` 每次新建 `AsyncOpenAI` 不 aclose，屬本檔既列的範圍外技術債，且非本次告警主因。

## 本輪零生產程式碼改動；唯一新增產出為一個「autoformat 寫回經 `_commit_push_merge` 不掉檔」守護測試
- 時間：2026-07-05 03:41
- 理由：需求四項行為已由 PR #282 覆蓋且測試綠；唯一真缺口是 `git add -A` 兜底帶檔的隱式契約，用測試顯式化
- 否決方案：重新實作或重構 lint 閘門——重造輪子且引入回歸風險

## 新測試開獨立檔（如 `tests/autopilot/test_qa_autoformat_writeback_committed.py`），不放進 `test_qa_no_publish_pollution.py`
- 時間：2026-07-05 03:41
- 理由：高工核實該檔有 autouse `_forbid_real_subprocess` 全面封殺真 subprocess，新測試需跑真 git 必被炸；只複用其 `_base_config` 設定範式
- 否決方案：沿用同檔＋monkeypatch 範式（我原案）——與 autouse fixture 直接衝突

## 測試走全真 git 路線——本地 bare repo 當 origin → clone → 模擬 autoformat 寫回 → 設 `AUTOPILOT_DRYRUN=True` → 走真 `_commit_push_merge` → `git show HEAD` 斷言寫回檔在 commit 內
- 時間：2026-07-05 03:41
- 理由：dryrun 在 push 前 return，天然避開 GitHub 段；全真 git 零 stub，比 monkeypatch push/PR 段更不脆（工程師與高工獨立得出同一路線）
- 否決方案：monkeypatch push/PR/merge 段（我原案）——多餘的 stub 面積，更脆

## fixture 必須滿足入口 guard 與中段查詢——設 `AUTOPILOT_REPO`、owner allowlist，且 clone 需有 `origin/main` ref（否則 `rev-list origin/<branch>..HEAD` 先掛）
- 時間：2026-07-05 03:41
- 理由：否則測試在 guard 或 rev-list 短路，測不到 staging/commit 段

## 斷言目標維持「寫回檔改動出現在 commit 內容」（行為契約），不斷言 `git add -A` 被呼叫
- 時間：2026-07-05 03:41

## 同步修正既有衝突——`test_gate_lint_autoformat.py` 中直接 assert `"add", "-A"` 的 AST 斷言段，改為（或由新測試取代後移除）commit 內容行為斷言
- 時間：2026-07-05 03:41
- 理由：工程師指出該 AST 斷言鎖實作，會卡死未來改選擇性 add；留著它，新行為測試形同虛設
- 否決方案：只加新測試、保留舊 AST 斷言——兩者並存自相矛盾

## 紅樣本自證為必要步驟，且結果同時記入 commit 訊息與測試 docstring
- 時間：2026-07-05 03:41
- 理由：高工建議——半年後讀測試檔即知驗過判別力，不用翻 git log

## 範圍裁決維持——gate/CI ruff 版本 pin 對齊本輪不做列移交待辦；不引入 `ruff check --fix` 進閘門（明文防翻案）；orchestrator 不加 lint 檢查點
- 時間：2026-07-05 03:41

## #1/#2 為純核對輸出（對照表＋覆蓋確認）；收尾 #4 以 `git status` 乾淨為準，除新測試檔與上述 AST 斷言修正外零 diff
- 時間：2026-07-05 03:41

## 題目附帶的 lessons/vote 去重既有決策與本需求無關，本輪不觸碰
- 時間：2026-07-05 03:41

## 注入語義改為方案 A——wrapper 對本角色（speaker key 相符）的**每一則** EXPERT_MESSAGE 過境時注入 `duration_s = monotonic() - t0`（自本次 speak 開始的累計耗時）＋ provider/model/role；同 turn 最後一筆 ≈ 整輪耗時
- 時間：2026-07-05 11:54
- 理由：全 codebase 無呼叫點設 `final=True`，且 Claude 串流路徑逐 block 廣播、事件過境當下無法預知是否最後一則——「只攔 final」在現況永不觸發，屬靜默失效
- 否決方案：方案 B（各後端終端訊息補 `final=True`）——fake/providers 可行但 experts.py 串流迴圈不知哪塊是最後一塊，需重構；且會造成 Claude 路徑永無 final 的分裂語義

## `final` 旗標本輪不動、不修其語義；一次 speak 多則訊息（串流 block／system note）皆帶累計耗時，此語義明載於 wrapper docstring
- 時間：2026-07-05 11:54
- 理由：final 旗標的語義修復是獨立議題，混進本輪會擴大範圍；累計耗時語義對多訊息天然自洽

## 注入點維持 wrapper 攔截傳入 speak 的 broadcast callable，就地 mutate `StudioEvent.payload`（mutable dict）；事件型別比對用 `events.EventType.EXPERT_MESSAGE` enum，不用字串
- 時間：2026-07-05 11:54
- 理由：零呼叫點侵入、可逆（拆 wrapper 即還原）；history 寫檔在 broadcast 之後，注入值會完整落檔
- 否決方案：改 speak() 回傳值帶 duration——動 ExpertLike Protocol 與全部呼叫點簽名

## expert_wrap.py 內加註解明講「依賴 broadcast → history 寫檔的順序」這條隱含耦合，防未來改 ws.py 的人踩坑
- 時間：2026-07-05 11:54

## provider 不用 getattr 猜——由建構點明傳：`make_expert()` 用已知的 `prov`、fake_experts 傳 `"fake"`；model 於注入當下解析：有 `effective_model()` 就呼叫（支援 per-task 覆寫的動態值），否則空字串
- 時間：2026-07-05 11:54
- 理由：各後端 model 屬性名分歧（`_model`/`_model_override`/`effective_model()`），純 getattr 屬性名會踩坑；effective_model 是既有的對外慣例（task_result 已用）
- 否決方案：純 `getattr(expert, "provider"/"model")` fallback 鏈

## wrapper 落在新中立模組 `studio/expert_wrap.py`（只依賴 events＋stdlib）；`providers.make_expert()` 回傳處與 `fake_experts.py` 全部建構點套同一函式；禁止 fake_experts import providers
- 時間：2026-07-05 11:54
- 理由：保護依賴方向——fake_experts 反向 import providers 會拖進全部 SDK 依賴

## 計時基準 `time.monotonic()`；duration_s 語義＝含 retry 退避的 wall-clock，明載 docstring；speak 拋例外→無事件可附掛→無 duration，接受並文件化，不加旁路事件；每次 speak 各自 t0，並行 lane 互不干擾
- 時間：2026-07-05 11:54

## 欄位單一寫入路徑＝只有 wrapper 注入；`events.expert_message()` 增 keyword-only optional 參數（None 即省略）僅供 schema 文件與測試建構；欄位一律 optional，舊 history 重播與 events-render.js 容忍缺省，本輪不加 UI 顯示
- 時間：2026-07-05 11:54

## 驗收測試必含**正樣本**——FakeExpert 包 wrapper 跑離線 e2e，斷言事件確實出現 `duration_s > 0` 且 `provider="fake"`；另補 Claude 串流路徑單元測試斷言多則訊息皆帶累計耗時且單調遞增；缺省容忍（白樣本）另測
- 時間：2026-07-05 11:54
- 理由：高工指出本次「條件永不觸發」正是只測白樣本抓不到的靜默失效——正樣本是判別力所在

## proxy 用 `__getattr__` 全轉發（name/avatar/role/stop/fake 的 calls 自然透傳），暴露 `.wrapped` 作 isinstance 逃生門
- 時間：2026-07-05 11:54

## 明文不做——不引入 wrapt/OTel SDK、不動 retry 內部、不做 per-attempt 粒度、不改 marker 字串、不修 final 旗標語義、不做 perf_counter 翻案；未來細粒度需求以新 optional 欄位擴充，不改 duration_s 既定語義
- 時間：2026-07-05 11:54

## 分散式去同步統計測試直接 import `studio.llm_caller.backoff_delay` 純函式驗證，不經 experts/config/orchestrator，測試依賴方向單向 `tests → llm_caller`
- 時間：2026-07-05 22:28
- 理由：backoff_delay 為純函式且已有 rand 注入縫（llm_caller.py:436），最低層驗證分佈最穩、零 mock，退避未來重構時測試是獨立事實來源
- 否決方案：端到端經 experts/config 驗證統計——experts 僅平鋪傳 config 值，該線用盤點證明即可，跑統計不值得

## N 客戶端模擬採「同一 (attempt, retry_after) 呼叫 N≥50 次 + 注入序列化 rand（預生成確定序列，如 i/N）」，禁用真 random
- 時間：2026-07-05 22:28
- 理由：統計斷言配真 random 必 flaky；序列化 rand 確保 CI 可重現
- 否決方案：真 random.random 抽樣

## 429 路徑測試選 retry_after 明顯低於 cap（如 10/60），529 路徑避免 attempt 已打到 cap
- 時間：2026-07-05 22:28
- 理由：上界被 cap 夾住會壓縮分散度使 jitter 區間失真，選值避開 cap 夾擠才驗得出去同步

## 429 斷言落點 ∈ [nominal, nominal·(1+j)] 且下界嚴格 = nominal；529 斷言落點 ∈ [nominal·(1-j), nominal]；兩路徑各補「非全等 + stdev>0」
- 時間：2026-07-05 22:28
- 理由：下界嚴格 = nominal 守驗收#6（jitter 不早於伺服器 retry-after）；非全等 + stdev>0 證去同步

## 除 N≥50 統計案例外，各路徑另補確定性邊界案例——429 用 rand→1.0（含 cap 夾擠 llm_caller.py:443）與 rand→0.0 驗上下端點，529 用 rand→接近1 驗最深退避不穿透
- 時間：2026-07-05 22:28
- 理由：統計案例證分佈、邊界案例證公式端點，兩者互補鎖死上下界，比純抽樣更硬
- 否決方案：只靠 N=50 抽樣驗端點

## 黑樣本斷言 jitter=0 時同 attempt 延遲全等，與白樣本同檔對照
- 時間：2026-07-05 22:28
- 理由：llm_caller.py:441/446 兩路徑 j==0 皆 early-return 確定值，黑樣本嚴格全等，證白樣本非假綠

## 測試檔頂註明 N 值與統計門檻（stdev>0、len(set)>1）的選定理由
- 時間：2026-07-05 22:28
- 理由：防日後有人調小 N 破壞判別力而不自知

## 新增前先去重既有 jitter/backoff 守門測試與文件，新增範圍控制在「小型統計測試 + 文件盤點修正」
- 時間：2026-07-05 22:28
- 理由：現況已有不少相關守門測試與文件，避免測試重複堆疊

## 任務#1 呼叫端盤點為文件產物（檔名:行號 + 狀態），證明唯一退避入口 make_retry_config→backoff_delay、jitter 實際值 = config.EXPERT_RATE_LIMIT_BACKOFF_JITTER，不引入程式依賴；行號須更新為實際值（如 _build_client 註解在 experts.py:373-378）
- 時間：2026-07-05 22:28

## 單層退避盤點對兩條 provider 路徑一律採「證據式」措辭而非「風險」——`providers.py:1167 max_retries=0（已解除疊乘）` 與 `experts.py:373-378（Claude 路徑無旋鈕、天然單層）` 並列
- 時間：2026-07-05 22:28
- 理由：OpenAI 疊乘已落實解除，寫成「風險」會誤導接手者去「修」一個已修好的東西
- 否決方案：將 OpenAI SDK max_retries 疊乘寫成未處理隱患

## SDK 疊乘（#4）以文件+盤點建構點行號佐證，不強制加守門測試
- 時間：2026-07-05 22:28
- 理由：這是常數性事實（max_retries=0、Claude 無旋鈕）非條件邏輯，守門測試保護不到會退化的東西
- 否決方案：一律為 SDK 不疊乘加守門測試

## 禁止為方便測試而修改 backoff_delay 簽章、marker 字串或 config 預設；所有產出 additive（新測試檔 + DECISIONS/註解），保持可逆與退避 SSOT 唯一
- 時間：2026-07-05 22:28

## 不引入 tenacity/backoff 等第三方套件，沿用自研退避骨幹為唯一 SSOT
- 時間：2026-07-05 22:28

## 【任務#4 查核結論】Claude Agent SDK retry 查核：已知邊界與現有防護
- 時間：2026-07-05
- 可控層已確認（Python SDK 原始碼）：`ClaudeAgentOptions.__init__`（參數列含 max_turns/model 等，無 max_retries）與 `ClaudeSDKClient.__init__`（只有 options/transport）均不含 retry 旋鈕；`query.py` / `subprocess_cli.py` 原始碼全文 grep 確認無 429/529 retry 邏輯。**Python SDK 可控層為單層退避。**
  - 建構點①（Claude 路徑）：`experts.py:373-381` — `_build_client` docstring 明文可控層邊界，保留「ClaudeSDKClient 本身不做額外退避，避免雙層疊乘」
  - 建構點②（OpenAI 路徑）：`providers.py:1167` — `openai.AsyncOpenAI(max_retries=0)` 顯式設 0；MiniMax/Gemini 共用同路徑，一次到位
- 已知邊界（CLI subprocess 層）：`claude_agent_sdk` 是 Claude Code CLI（Node.js）的 subprocess wrapper。CLI 最終確實透傳 API 429/529（`types.py:api_error_status`），但 CLI 在透傳前是否做內部 retry 不可從 Python SDK 原始碼驗證。若 CLI 有內部 retry，Ti 的 `run_with_retries` 加上去仍是疊乘——差別是 CLI 層有個未知上界，且最終錯誤必然浮出。此為已知邊界，非已知確認安全。
- 回歸守門：`tests/core/test_claude_no_double_backoff_task3_qa.py`（AST 確認 Python 層無 retry 旋鈕；三測試：書面結論、無 retry kwarg、speak 路徑唯一退避權威）
- 不加額外守門測試的理由：CLI 內部行為不在 Python SDK 可測範圍，AST 守門已封住「在 Python client 層再加旋鈕」這條退化路徑；CLI 層風險屬架構邊界，非可用測試守護的條件邏輯


## 【任務#5 收尾記錄】jitter 消費端 default-on＝0.5 與去同步黑白樣本測法
- 時間：2026-07-05
- **jitter 消費端 default-on＝0.5（非未接線）**：`config.EXPERT_RATE_LIMIT_BACKOFF_JITTER = _env_float("TI_RATELIMIT_BACKOFF_JITTER", 0.5)`（`studio/config.py:443`，`reload()` 於 `config.py:1203` 同鍵複寫）。`llm_caller.DEFAULT_BACKOFF_JITTER=0.0` 僅為**函式庫層預設關閉**，消費端 `experts.make_retry_config()` 已覆寫為 0.5（`experts.py:132`）並 lazy-read（`experts.py:96`）。→ 本需求**九成已落地**，本場重心為「補分散式去同步統計驗證＋封口串流無旁路退避」，非從零開啟 jitter。
- **無旁路盤點（任務#1）**：`studio/docs/jitter_backoff_inventory.md` 逐一標出 orchestrator/experts 串流路徑所有退避呼叫點（C1/C2/O1/O2/Orc1–Orc4，含 `complete_once` wrapper），證明唯一退避入口＝`make_retry_config`→`backoff_delay`，無第二條繞過 jitter 的退避。
- **去同步驗證黑白樣本測法（任務#2/#3）**：`tests/core/test_backoff_desync_task2_qa.py`，N=64 客戶端同 attempt、注入序列化 rand（`i/(N-1)`，禁真 random 保 CI 可重現）。
  - 白樣本（jitter=0.5）：429（有 retry-after）向上散、529（無 retry-after）向下散，各斷言 (a) 非全等 `len(set)>1`、(b) `pstdev>0`、(c) 全落理論帶內（429∈`[nominal, nominal·(1+j)]` 且下界嚴格＝nominal 守驗收#6；529∈`[nominal·(1-j), nominal]`）。
  - 黑樣本（jitter=0，同檔對照）：同 attempt 延遲**退化全等** `len(set)==1`、`pstdev==0`、值＝nominal，證白樣本「非全等」非假綠。
  - RetryConfig 層另有 jitter=0 確定值黑樣本於 `tests/core/test_retry_config_task4_qa.py`。
- **SDK 不疊乘（任務#4）**：Python SDK 可控層無 retry 旋鈕（`experts.py:373-381`、`providers.py:1167 max_retries=0`），守門 `tests/core/test_claude_no_double_backoff_task3_qa.py`；CLI subprocess 層是否內部 retry 屬已知邊界（見上方任務#4 查核結論）。
- **收尾驗證**：`.venv/bin/python -m pytest -q` 全綠、`.venv/bin/python -m ruff check .` 無錯（新測試在其中）。
## 本場定性為驗證封口，範圍限 additive 產出（測試斷言／文件／移交待辦），禁止修改 CHANGELOG 內容、`BREAKING_HEADING` 常數、`render_release_body` 簽章及任何 marker 字串
- 時間：2026-07-06 00:11
- 理由：內容改一字可能動到四要素順序契約，換來零回歸與可逆性
- 否決方案：順手潤飾 CHANGELOG 或改常數遷就內容

## 資料流沿用現況不變——CHANGELOG（內容 SSOT）→ `release_note.extract_breaking_block`（錨＝`BREAKING_HEADING`）→ `publish_release.render_release_body` → `body.md`，本場只沿此鏈實跑取證
- 時間：2026-07-06 00:11

## `release_note.BREAKING_HEADING` 為抽取錨點單一事實來源，依賴方向固定為「CHANGELOG heading 對齊常數」；若查出漂移一律改 `CHANGELOG.md`，不得反向改常數
- 時間：2026-07-06 00:11

## #2 交付為文件級逐字比對證據，不新增與 `four_elements_in_order`／`breaking_is_at_top` 重疊的測試
- 時間：2026-07-06 00:11
- 理由：現況守門測試已覆蓋，避免測試堆疊
- 否決方案：為求安全感新增比對測試

## #2 逐字比對證據須指名到出處路徑——貼出 `release_note.BREAKING_HEADING` 常數與 `CHANGELOG.md` 實際 heading 兩邊來源的檔名行號，供接手者重驗漂移
- 時間：2026-07-06 00:11
- 理由：高工提醒「已比對」一句話不可勾稽，具名出處才能重驗

## #2 須實跑 mutation 證明改 heading 會翻紅，且判準寫死為「必失敗於 `four_elements_in_order` 或 `breaking_is_at_top`」，而非任意 collection/import error
- 時間：2026-07-06 00:11
- 理由：靜態看兩處相等是假綠重災區；typo 讓 import 爆掉不算驗到契約
- 否決方案：只靜態比對兩處相等即結案

## warn/off 維持「使用者側逃生艙、即刻生效」語氣，禁止引入 deprecation 過渡期或未來版本才 enforce 措辭，否則 `has_future_enforce_timeline` 翻紅且違反 ADR
- 時間：2026-07-06 00:11
- 否決方案：採業界「先 deprecated 數版警告再 breaking」過渡策略（須先翻 `DECISIONS.md:791,1000`）

## #3 明確標註「真實 v* tag-push 生產 E2E」為未閉環移交待辦，不以單元/守門測試假裝已閉環
- 時間：2026-07-06 00:11
- 理由：記憶明載半閉環，驗證報告不得過度宣稱

## #3 人工確認清單須可勾稽，明列具名步驟「發 release 後開 `body.md` 確認 breaking 區塊在頂部」，不得只寫「人工確認」
- 時間：2026-07-06 00:11
- 理由：高工提醒無具名步驟等於沒交

## 全部產出 additive、零 production code 變更、可逆（新測試斷言＋文件盤點＋移交待辦），退避／marker SSOT 保持唯一
- 時間：2026-07-06 00:11

## #2 假綠封口採「新增獨立斷言」而非改動 `FOUR_ELEMENTS` 生效版本 regex
- 時間：2026-07-06 00:51
- 理由：`FOUR_ELEMENTS`／`missing_elements()` 簽名與語意保持不變，維持零 production 變更與可逆；職責分離（既有尺規管結構、新斷言管版本值）
- 否決方案：把 `FOUR_ELEMENTS[3]` 語意 regex 插值 `pyproject_version()`——會迫使 `missing_elements()` 吃 version 參數、擴散到 task-3/task-4 兩檔並改到共用 production 尺規語意

## 新增版本對應 helper（如 `version_matches_effective(body, version)`）落在 `tests/autopilot/_release_check.py`，作為兩出口尺規的同一單一事實來源，不新增第二份版本判定邏輯
- 時間：2026-07-06 00:51
- 理由：task-3／task-4 已共同 import `_release_check`，放這裡一次餵到兩檔，避免尺規散落漂移

## helper 必須錨定「④ 生效版本」那句，用 `④\s*生效版本[^\n]*自\s*`?{re.escape(version)}`?\s*起`，禁止 `version in body` 子串比對
- 時間：2026-07-06 00:51
- 理由：body 的 heading／footer／③ before-after 已出現 `0.2.0`，子串比對下把 ④ 行改舊版仍含 `0.2.0`→不翻紅，假綠復活；`polluted != changelog` 只擋空操作、擋不住這個

## helper 比對前對 `version` 做 `re.escape()`
- 時間：2026-07-06 00:51
- 理由：版本含 `.`，未跳脫時 `0.2.0` 會誤配 `0x2y0` 之類，稀釋鑑別力

## 新版本對應斷言須套在 tag_notes／email_banner 兩個出口 body，與 `missing_elements()` 同待遇
- 時間：2026-07-06 00:51
- 理由：只驗單一出口＝尺規半殘，另一出口仍可版本漂移

## helper docstring 明寫「本函式補 `FOUR_ELEMENTS[3]` 對版本值不敏感的缺口」，交代兩把尺分工
- 時間：2026-07-06 00:51
- 理由：既有 ④ 語意 regex 有 `|生效版本` 分支（有四字無版本號也算過），讓接手者一眼懂分工、不誤刪其一

## 版本權威唯一來源固定為 `release_note.pyproject_version()`，renderer 傳入版本與 body 內 ④ 句同源勾稽，任一失配翻紅
- 時間：2026-07-06 00:51

## 依賴方向不變——CHANGELOG heading 對齊 `BREAKING_HEADING`、版本對齊 pyproject；查出漂移一律改 CHANGELOG，禁止反向改常數或硬寫版本字面值
- 時間：2026-07-06 00:51

## #2 黑樣本只 mutate CHANGELOG 副本「④ 生效版本」行的版本字串（0.2.0→舊版如 0.1.9），renderer 傳入版本維持權威 0.2.0，成對自證 baseline 綠→mutation 紅，並斷言 `polluted != changelog` 排除空操作
- 時間：2026-07-06 00:51

## #2 黑樣本判準寫死為「新版本對應斷言失敗」，非任意 collection/import error，避免 typo 假綠
- 時間：2026-07-06 00:51

## #3 離線核對 body.md 時，因 `scripts/publish_release.py` CLI 固定寫 repo root，改用其底層 `render_release_body()` 寫 `$TMPDIR`；若堅持走 CLI，則跑完立即刪除 repo root `body.md` 並以 `git status` 驗無殘留
- 時間：2026-07-06 00:51
- 理由：工程師指出 CLI 不支援 `$TMPDIR`，硬要臨時檔不落 repo 會與 CLI 行為衝突，須二擇一明確落地
- 否決方案：直接讓 body.md 落在 repo root 不清理——會造成 pre-commit 綠／CI 紅分歧、違反臨時檔不落被掃描目錄的硬規則

## #4 明文標註「真實 v* tag-push 生產 E2E 為半閉環未閉環」並列具名人工核對步驟（發 release 後開 body.md 確認 Breaking 區塊置頂、含四要素與 `TI_REQUIRE_CHOWN=warn/off` 逃生艙），不以單元/守門測試假裝已閉環
- 時間：2026-07-06 00:51

## 全部產出 additive、可逆、零 production code 變更（新測試斷言＋文件盤點＋移交待辦）；warn/off 維持即刻生效逃生艙語氣，不引入 deprecation 過渡措辭
- 時間：2026-07-06 00:51

## 線上核對定位為「一次性人工/腳本動作、其輸出為產物」，禁止把打 live GitHub 的網路測試加入測試套件
- 時間：2026-07-06 01:42
- 理由：CI 測試慣例是離線假專家、不綁外部 API；live 網路測試會 flaky 並把生產可用性綁進 CI，鎖死未來
- 否決方案：新增一個實際打 GitHub API 的自動化核對測試放進 CI

## 分兩把尺——live 事實用 `gh`+REST 交叉驗證於本次執行完成；守門測試 `test_qa_task4_e2e_handoff.py` 只離線驗「文件契約」，不驗主張為真
- 時間：2026-07-06 01:42
- 理由：文件契約可離線確定性驗證；主張真偽屬 live 事實，混為一談會讓測試依賴網路

## body 核對主用「結構斷言」（頂部第一個 `## ` 逐字為 `## ⚠️ Breaking Changes`＋四要素＋`TI_REQUIRE_CHOWN=warn/off`），正規化後逐字比對僅為本次核對腳本的輔助，不進測試套件
- 時間：2026-07-06 01:42
- 理由：逐字比對受 GitHub 換行正規化影響易假性不符；且逐字比對責任若進 CI 會長期脆弱
- 否決方案：把「正規化後逐字比對」做成常駐守門測試

## 雙來源交叉驗證——`gh release view --json body` 為主、REST `GET /repos/.../releases/tags/v0.2.0` 的 `body` 為第二來源，各自 `sed 's/\r$//'`＋去尾空行正規化後須相等
- 時間：2026-07-06 01:42

## 證據回填進 `docs/release-e2e-handoff.md`，不新開證據檔；「證據檔」即更新後的 handoff doc
- 時間：2026-07-06 01:42
- 理由：單一權威、防文件漂移，與既有「doc 是移交明文」決策一致
- 否決方案：另建獨立證據 artifact 檔

## 翻 ✅ 的同時，必須同步收斂檔案頂部「半閉環聲明」使其與 B 段一致——聲明改為「v0.2.0 此鏈已生產閉環，後續版本仍須逐項複核」，消除文件自我矛盾
- 時間：2026-07-06 01:42
- 理由：只翻 B 段 ❌→✅ 而不動頂部「仍為半閉環」聲明，交付即是內部打架的文件，構成漂移
- 否決方案：只改 B 段兩列、保留頂部原半閉環聲明

## 證據須標明 failure run `27905351284` 的觸發事件為 `release`（同 tag 較早 release 實例、後被取代重建），並據此論證兩筆 run 皆 `release` 事件 → 雙重確認觸發可靠，非反證閉環
- 時間：2026-07-06 01:42
- 理由：只並列不分類，讀者無法判斷 failure 是否反證「release 事件可靠觸發」；查清來源才是把僥倖說清楚
- 否決方案：只「誠實並列」failure run 而不交代其觸發來源

## smoke 閉環證據引用對應現行 release（id `342528036`／createdAt 13:15:15）的 success run `27905531397`，並註明擷取時點與 run-id 為一次性 artifact
- 時間：2026-07-06 01:42

## 守門測試須補成對黑樣本自證判別力——竄改 run-id／`✅` 翻回 `❌`／抽掉 failure run 記載任一情形，測試須轉紅
- 時間：2026-07-06 01:42
- 理由：「run-id 字串在、✅ 在」過於寬鬆，字串存在不等於判別力，違反本 repo 自證對應＋排除假綠硬規則
- 否決方案：守門測試僅正向斷言關鍵字存在、無黑樣本

## 守門測試錨定 B 段或新證據節、不整份逐字比對，措辭須明確表達「文件記錄了此主張」而非「主張為真」
- 時間：2026-07-06 01:42
- 理由：整份逐字比對過脆易誤紅；措辭無歧義才不會被誤讀為 CI 已驗生產真偽

## 全部產出 additive／可逆／零 production code 變更，`BREAKING_HEADING` 常數與版本字面值不動，查出漂移一律改 `CHANGELOG.md`，不加 `--verify-tag`（沿用既有決策）
- 時間：2026-07-06 01:42

## 新增 `docs/evidence/release-smoke-v0.2.0-trigger.json` 為 smoke 觸發證據唯一 SSOT，比照既有 `docs/evidence/release-v0.2.0-online-body.json` 的欄位風格（雙路命令＋關鍵欄位快照）
- 時間：2026-07-06 02:23

## evidence 採最小固定 schema——`verification_status`、`success_run`、`superseded_failure_run`、`gh_run_view`、`rest_run`、`cross_checks`，不抽泛用 validator
- 時間：2026-07-06 02:23
- 理由：一次性封口證據，泛用框架的複雜度自證不了，YAGNI
- 否決方案：先做泛用 evidence schema validator

## evidence `verification_status: verified|pending` 為狀態 gate；handoff 翻 ✅ 條件蘊含其為 `verified` 且 `event=release`／`conclusion=success`，由守門測試強制此關係
- 時間：2026-07-06 02:23
- 理由：把誠實性編成測試約束，不靠人自律，假綠在測試層擋死
- 否決方案：靠 codex 自律「別手抄」

## run-id `27905531397`/`27905351284` 不得預先寫死為既成事實；evidence 的 run-id 欄僅在本 session 真實 `gh` 擷取後填入，並附 provenance（擷取命令＋時點）
- 時間：2026-07-06 02:23
- 理由：高工退回點——「跑不了就 pending」與「把 success run-id 當事實」不能並存，未實證的 success 數字正是本任務要擋的假綠
- 否決方案：把兩個 run-id 當調研既成事實直接寫入設計/證據

## 未完成 `gh` 實跑擷取時，evidence 一律 `verification_status: pending`、不得出現任何 success run-id、handoff 維持 ❌、標「待有權限者核對」
- 時間：2026-07-06 02:23
- 理由：沙箱網路白名單無 GH auth，實跑很可能受限；此為誠實性 fallback

## 證據雙來源＝`gh run view <id> --json databaseId,event,status,conclusion,headBranch,url,createdAt` 與 REST `gh api repos/x812033727/Ti/actions/runs/<id>`，兩路關鍵欄位須相等並記入 `cross_checks`
- 時間：2026-07-06 02:23

## smoke 證據綁定對象為 `release-smoke.yml` 的 workflow run 本身（其 `databaseId`／`createdAt`／`conclusion`），不得沿用 release 物件的 `id=342528036`／`created_at=13:15:15`
- 時間：2026-07-06 02:23
- 理由：高工退回點——release 物件與 workflow run 是不同 artifact，貼 release 物件 id 到 smoke 證據即資料錯誤

## evidence 同時記 success run 與被取代的 failure run（`superseded_failure_run`），兩者標 `event=release` 並註明 failure 為同 tag 早期 release 被取代重建，論證觸發可靠雙重確認而非反證
- 時間：2026-07-06 02:23
- 理由：把僥倖說清楚（跨場次硬規則），只報 success 藏 failure 是不誠實

## 頂部半閉環聲明改版本限定收斂——「v0.2.0 此鏈已生產閉環；後續版本仍為半閉環、尚待逐版生產驗證」，措辭須完整保留 `真實/tag-push/端到端/生產驗證/半閉環/尚待` 關鍵詞
- 時間：2026-07-06 02:23
- 理由：既有 `check_half_closed` 與 `test_mutation_soften...` 為全域字串 replace，保留關鍵詞即零改動繼續綠，保護既有護欄依賴方向
- 否決方案：一刀切改「已完整驗證」——會打爆既有守門測試

## 範圍擴至「body 置頂」那條一併翻 ✅，依據引用既有 `release-v0.2.0-online-body.json`（不新增證據），使頂部聲明宣稱 v0.2.0 全鏈閉環時文件自洽；PM 未定前措辭浮動，fallback 為逐環標註不宣稱全閉環，收斂時實跑 `check_half_closed` 相關測試確認關鍵詞未軟化
- 時間：2026-07-06 02:23
- 理由：只翻 smoke 留 body 置頂 ❌ 會與「全鏈閉環」聲明自相矛盾；既有 online-body evidence 已足
- 否決方案：PM「範圍極簡只封 smoke 一條」（暫留為 fallback，待 @pm 拍板）

## 守門測試新建獨立檔 `tests/autopilot/test_qa_smoke_trigger_evidence.py`，只驗 JSON snapshot 內兩路欄位一致與 handoff run-id／✅／❌ 對 JSON gate 一致，不重跑 `gh`、不打網路，錨定新證據節不整份逐字比對
- 時間：2026-07-06 02:23

## 守門測試成對黑樣本任一須翻紅——竄改 run-id／✅ 改回 ❌／抽掉 `event` 欄／抽掉 failure run 記載／`verification_status` 退回 pending 但 ✅ 未退
- 時間：2026-07-06 02:23

## 改 handoff L26 時保留 `test_task4_commit_does_not_alter_release_smoke_trigger` symbol 引用，避免既有 `test_handoff_symbol_references_are_live` 翻紅
- 時間：2026-07-06 02:23

## 全部產出 additive／可逆／零 production code 變更，`BREAKING_HEADING` 常數與版本字面值不動、不加 `--verify-tag`
- 時間：2026-07-06 02:23

## body 置頂列只改單列（狀態 ⏳→✅＋依據欄補證據路徑），smoke 列維持不動、僅核對
- 時間：2026-07-06 03:14

## 落地一律用內容錨點定位（列首粗體字串／`BREAKING_HEADING` 常數／符號名），禁寫死行號
- 時間：2026-07-06 03:14
- 理由：本 repo 既有教訓「行號會漂」，設計指位的 L9/L29/L30 實際已漂移為 L7-18；錨點才穩定
- 否決方案：靠 L29/L30 行號 diff——文件前段增減行即誤改鄰列

## evidence 三份 JSON 唯讀引用、零新增證據檔，文件宣稱單向依賴證據事實
- 時間：2026-07-06 03:14

## 保護依賴方向——禁止為文件自洽反向改 `BREAKING_HEADING`／版本字面／evidence 欄位
- 時間：2026-07-06 03:14

## 翻 body 列 ✅「前」必須先補一支對稱 pytest 守護，使 body 列 ✅ 與 smoke 列 ✅ 同級護欄
- 時間：2026-07-06 03:14
- 理由：smoke 列 ✅ 背後有 CI 跑的守護測試、竄改即紅；`check_release_body_structure.py`+`online-body.json` 目前無任何 tests/ 引用、CI 一次不跑，翻 ✅ 等於無護欄真理宣稱，evidence 被動或 CHANGELOG 結構漂移都不會翻紅
- 否決方案：直接引 script+靜態 json 當依據就翻 ✅——比 smoke 列弱一級證據，撐不起同一顆 ✅（高工退回點）

## 新守護獨立建檔 `tests/autopilot/test_qa_body_pinning_evidence.py`，對稱 smoke 的 `test_qa_smoke_trigger_evidence.py`
- 時間：2026-07-06 03:14
- 理由：沿用「每個生產級宣稱配一支對稱獨立守護」既有模式，依賴方向一致、後人易懂；多一檔成本低於把 body 斷言塞進 `test_qa_task4_e2e_handoff.py` 造成職責混雜
- 否決方案：斷言併入既有 handoff 測試——範圍雖更小，但該檔職責（守半閉環聲明）會被稀釋

## 新守護斷言 `online-body.json` 的 `body_match=true`、頂部第一個頂層區塊＝`BREAKING_HEADING` 常數、四要素與 `TI_REQUIRE_CHOWN=warn/off` 逃生艙齊，並配成對黑樣本（頂部非 Breaking／抽掉要素即翻紅），可直接把 verdict 的 `black_sample_selfcheck` 包成 collect
- 時間：2026-07-06 03:14

## body 列依據欄除三路徑外，加引這支新守護 test，才與 smoke 列 ✅ 依據同級
- 時間：2026-07-06 03:14

## 頂部聲明擴寫為並列 smoke＋body 兩環為「v0.2.0 已具生產證據之閉環」，`尚待／半閉環` 只保留給「後續版本／其餘未具證據環節」
- 時間：2026-07-06 03:14

## 頂部聲明完整保留 `真實／tag-push／端到端／生產驗證／半閉環／尚待` 六關鍵詞（實際 `check_half_closed` 為四必要詞＋修飾詞，保六為安全冗餘）
- 時間：2026-07-06 03:14
- 理由：`test_mutation_soften` 是全域 replace 所有 `尚待/半閉環` 才驗紅，版本限定收斂可 baseline 綠、mutation 仍紅

## 執行順序調整為 先補守護測試 → #3 聲明收斂 → #1 翻列 → #4 驗證，避免「表閉環／頭待封」矛盾中間態且翻列前護欄已就位
- 時間：2026-07-06 03:14

## 範圍鎖定——不加 `--verify-tag`、不鎖 actions SHA、不碰 smoke 列與「gh+REST 雙路」既有字串，供應鏈硬化留待辦
- 時間：2026-07-06 03:14

## 全產出 additive／可逆／零 production code 變更（新增守護測試屬 additive 護欄、不算 production 變更）
- 時間：2026-07-06 03:14

## 驗收指令補入新檔 `test_qa_body_pinning_evidence.py`，與既有三支守護測試一併為收斂閘
- 時間：2026-07-06 03:14

## 不引入任何外部模糊比對庫（RapidFuzz／thefuzz），複用 repo 內既有 `_token_set_similarity`（詞集 Jaccard）
- 時間：2026-07-06 04:25
- 理由：GPL 授權污染、Levenshtein 對 CJK 逐字不適配、額外分詞依賴三點皆不划算；輪子已在 autopilot.py
- 否決方案：引入 RapidFuzz（MIT）或 stdlib difflib.SequenceMatcher——前者仍需配 jieba 分詞，後者字元序列比對不如既有 token-Jaccard 適合中英混排標題

## 從 `_filter_pending_duplicates` 第一道相似度層外提共用 helper `_first_similar_title(title, corpus) -> str | None`，回傳命中標題供 debug log，pending 與 done 兩處共用
- 時間：2026-07-06 04:25
- 理由：零行為變更的直接外提（autopilot.py:1002-1009），杜絕第二套實作漂移
- 否決方案：抽成 `_is_semantically_dup(...) -> bool`——回傳 bool 會丟失「近似哪一筆」的 log 資訊

## 共用 helper 只抽「相似度層」，不含「子系統廣度」第二道防線——done-list 去重僅套相似度，廣度防線維持 pending 專屬
- 時間：2026-07-06 04:25
- 理由：子系統廣度是針對 pending 過載語意，套到 done 會語意污染

## `_first_similar_title` 簽章收 `Iterable[str]`（不限死 list），維持 corpus 迭代順序、「第一個命中即短路」不變，且 helper 內不對 corpus 排序
- 時間：2026-07-06 04:25
- 理由：pending 逐位不變是 #1 硬線；done 的 `done_titles` 為 set 無序，多重命中回傳哪一筆不確定，但只影響 log 不影響 `is None` 過濾判定，可接受；排序是無收益擾動

## 共用 helper 及 dedup 家族一律留在 `studio/autopilot.py`，不搬遷至 `flow.py`
- 時間：2026-07-06 04:25
- 理由：`_normalize/_tokenize/_token_set_similarity/_filter_pending_duplicates` 家族已定居 autopilot.py，`improver` 沿用既有 `autopilot._xxx` 呼叫慣例；搬遷可逆但無收益、且碎裂內聚

## done 相似層門檻沿用既有 `config.AUTOPILOT_DEDUP_RATIO`，不新增 done 專屬門檻或開關，遵守 config SSOT
- 時間：2026-07-06 04:25
- 理由：兩防線真正同構；附帶提醒該常數在 config.py:879 為硬寫非 env，本輪範圍外不動

## done 相似層開關沿用既有 `config.AUTOPILOT_EVAL_MEMORY`——`=0` 時 `recent_done_titles` 回空 corpus 使 helper 全回 None，與舊精確比對 `=0` 逐位等價，向後相容不加分支
- 時間：2026-07-06 04:25

## `improver._discover` line 414 **只**替換後半段 `not in done_titles` 為 `autopilot._first_similar_title(...) is None`，前半段 `t["title"].strip()` 真值守衛必須保留
- 時間：2026-07-06 04:25
- 理由：空標題應由真值檢查擋掉，不讓空字串流進 helper 靠 `_token_set_similarity` 回 0.0 兜底——語意上空標題本就該丟

## done 過濾維持就地縮減 `items`，`dropped = raw_n - len(items)` 自動涵蓋 done-相似層新擋下項，不加額外計數（驗收 #6）
- 時間：2026-07-06 04:25

## 測試補三組即足——pending 回歸（逐位不變，以既有 dedup 測試為閘）、done `EVAL_MEMORY=0` 改寫版放行、done `EVAL_MEMORY>0` 改寫版被擋
- 時間：2026-07-06 04:25
- 理由：`=0`／`>0` 成對黑白樣本同時證「開關有效」與「相似度非精確」，是關鍵驗收閘；走離線假專家、不打外部 API

## 本輪只做觀測層——量測 `ttft_s` + 真 API A/B 驗證命中 + 記錄 prefix 失效清單；不換 SDK、不加手動 cache_control
- 時間：2026-07-06 05:17
- 理由：需求字面「加 caching」的前提已被研究員與程式碼證偽——此路徑快取已自動運作，可做的是量測與驗證
- 否決方案：改走原生 anthropic SDK 手動注入 cache_control（需重寫整個工具迴圈、鎖死未來、不可逆，列另案）

## 技術選型維持 `claude_agent_sdk`（ClaudeSDKClient/ClaudeAgentOptions），不引入原生 anthropic SDK、不新增依賴，prompt caching 依賴 Agent SDK 自動 breakpoint
- 時間：2026-07-06 05:17

## `ttft_s` 埋點放 `experts.stream_to_events` 串流迴圈，分母 = 進迴圈首個 `__anext__` 前的 `loop.time()`（共用既有單調鐘），分子 = 首個含內容訊息到達時刻
- 時間：2026-07-06 05:17
- 理由：單點內聚、零跨層改動；共用 :424 既有 `loop` 零成本
- 否決方案：追到 speak 層 `client.query()` 精確送出時戳（需跨層傳時間戳、汙染簽章）

## ttft 落點必須在 `collected.append(text)`（:469）之後，而非 `text` 一非空即記；ToolUseBlock 當首內容則直接算
- 時間：2026-07-06 05:17
- 理由：首個 TextBlock 會先過 `_classify_api_text`，命中即 raise（限流/overload）——埋在 append 後才不會把「其實是錯誤文字」誤記為首 token，語意乾淨

## 判斷「首個含內容訊息」用保守 helper，不硬依賴 SDK class 名稱——TextBlock 需非空文字、ToolUseBlock 直接算內容
- 時間：2026-07-06 05:17

## `ttft_s` 型別為 `float`（秒），與既有 `duration_api_ms`/`duration_ms`（int 毫秒）並存不取代，語意正交（整段 API 時延 vs 首 token 延遲）；PR 描述須一句點明型別/單位刻意不同，防後人誤「統一」
- 時間：2026-07-06 05:17

## 介面沿用既有縫——`_emit_claude_token_usage` 新增 `ttft_s: float | None` 參數，`events.token_usage` 新增 `ttft_s: float | None = None` 關鍵字參數，比照 `duration_ms`/`task_id` 僅「非 None 時寫入 payload」，不動既有欄位名；同步更新所有呼叫點與測試
- 時間：2026-07-06 05:17

## 向後相容——history 重播、`/api/metrics` 聚合、前端 `events-render.js` 一律以 `.get("ttft_s")` null-safe 讀取；聚合層本輪不對 ttft 做平均/統計（屬另案），只保證舊 JSONL 不報錯
- 時間：2026-07-06 05:17

## 模組邊界為純觀測——不碰 `flow.py`（無新 marker 解析）、不碰 config SSOT；A/B 用既有 `DISABLE_PROMPT_CACHING` 環境變數做 before/after，不新增 `TI_*` 旋鈕
- 時間：2026-07-06 05:17
- 否決方案：為 A/B 新增專屬 config 開關（既有 env 已足，新增即違反 config SSOT 且增維護面）

## A/B 實驗以 `DISABLE_PROMPT_CACHING=1`（before）vs 預設（after）為唯一乾淨對照，量測期固定 model/effort/system_prompt/CLI 版本鎖住 prefix；命中證據為 after 的 `cache_read_input_tokens > 0`，且報告須確認該 env 確被 Agent SDK 吃到
- 時間：2026-07-06 05:17

## 報告誠實標示——`ttft_s` 絕對值含「query→進迴圈」固定 offset，不可宣稱為真 TTFT，僅 before/after delta 有效（offset 相減抵消）；真 API 端到端屬半閉環，須標示離線 vs 真 API
- 時間：2026-07-06 05:17
- 理由：分母設在 stream_to_events 進入點、早於 speak 層 query 送出，若 query 非惰性則絕對值系統性少算固定量，但 A/B delta 不受影響

## 三個測試為核可前提（非另案），走離線假專家沿用既有注入縫——① 有內容訊息 → `ttft_s` 為正 float 且進 payload；② 只有 ResultMessage 全程無內容 → `ttft_s` 留 None → payload 不含該鍵；③ 舊 JSONL（無 ttft_s）重播 + `/api/metrics` 聚合 null-safe 斷言
- 時間：2026-07-06 05:17

## Claude provider 走 `claude_agent_sdk` 路徑無法手動加 `cache_control`；prompt caching 已由 Agent SDK 自動生效，本輪不引入手動控制（列另案）
- 時間：2026-07-06 05:17
- 背景：本專案 Claude 專家經 `studio/experts.py` 的 `ClaudeSDKClient`/`ClaudeAgentOptions`（包 Claude Code CLI subprocess），非原生 `anthropic` SDK。`ClaudeAgentOptions` 未暴露 cache_control/cachePoint 注入旋鈕（官方 open 功能請求 anthropics/claude-agent-sdk-python#626，無 workaround），故需求字面「為 system_prompt/tool 定義加 caching」對此路徑是錯誤前提。
- 事實：快取「已自動在跑」——Claude Code/Agent SDK 預設對 `tools → system → project context` 這段 prefix 自動放 ephemeral cache breakpoint（訂閱預設 1h、API key 預設 5m TTL）。`experts.py` 已擷取 `cache_read_input_tokens`/`cache_creation_input_tokens`，第一次請求為 creation（寫入、慢），後續同 prefix 為 read（~0.1x 成本、快），效果本就可驗證。
- 決策：本輪只做觀測層（量 `ttft_s` + 真 API A/B 驗證命中 + 記錄 prefix 失效清單），不手動注入 cache_control。
- 否決方案：改走原生 `anthropic` SDK 手動注入 cache_control（放大工具定義快取、指定 1h TTL 斷點）——需重寫整個工具迴圈、脫離 Agent SDK 的工具迴圈、成本高且不可逆，列為獨立評估的另案，不混進本任務。

## prefix 失效清單——會使 Agent SDK 自動快取全失效（進而拉高 TTFT）的變因，量測期須全部鎖死
- 時間：2026-07-06 05:17
- 失效變因（改動即讓後續請求 prefix 不同、快取重建）：
  1. 切換模型（model）。
  2. 切換 reasoning effort。
  3. 修改 system_prompt 或工具集（allowed_tools 增刪）。
  4. 升級 Claude Code CLI 版本（量測期間勿升級）。
  5. system_prompt 內動態段：cwd、git status、memory/檔案路徑——跨工作目錄/跨機器每個 prefix 不同、互不命中。
  6. 子代理（subagent）快取預設 5m TTL，且曾被官方 hardcode 關閉（claude-code#29966），子代理路徑命中率須獨立看待、不可假設與主代理一致。
- 相關環境變數旋鈕（皆為 Agent SDK/CLI 既有，非本專案 `TI_*`）：`DISABLE_PROMPT_CACHING`（本輪 A/B 的 before 開關）、`ENABLE_PROMPT_CACHING_1H`、`FORCE_PROMPT_CACHING_5M`。
- 跨 session/跨機器共用快取抑制建議：抑制 system_prompt 的動態段（cwd/git status/memory 路徑），使各工作目錄 prefix 一致才可能互相命中（官方 fleet 建議 exclude_dynamic_sections / modifying-system-prompts）；此屬另案優化，本輪僅記錄清單。
- 量測含意：A/B 期間若切模型/effort、或在升級 CLI 前後對比，TTFT 前後差可能來自 prefix 失效而非快取本身，結論即被污染——故命中證據以 after 的 `cache_read_input_tokens > 0` 為準，且固定 model/effort/system_prompt/CLI 版本。
- 來源：anthropics/claude-agent-sdk-python#626、anthropics/claude-code#29966、Claude Code prompt caching 文件、API prompt caching 文件。

## 心跳維持 `_write_status` → `status.json` 單一真相源，新增 `current_expert`(str|null)/`turn_started_at`(float|null) 併入既有扁平 payload；不新增檔案、機制或 `TI_*` 旋鈕
- 時間：2026-07-06 09:29
- 理由：基礎設施已存在，真正缺口只有專家粒度與事件驅動刷新，補欄位比重造風險小且可逆
- 否決方案：另開新狀態檔或引入 OTEL/Langfuse——與 repo「不隨意新增依賴」相悖，且輕量 JSON 心跳已足夠

## `/api/autopilot` 不改邏輯，沿用「原樣吐 status.json 當 heartbeat」，新欄位隨 payload 自動曝露；前端 timeline 以 null-safe `.get()` 讀 heartbeat，顯示 `current_expert` 與 `now - turn_started_at` 已跑時長
- 時間：2026-07-06 09:29

## turn 邊界資訊的接縫定在 autopilot `run_one_task` 的 `broadcast` callback（tap 層），禁止改 `orchestrator.py`/`experts.py`/`events.py`
- 時間：2026-07-06 09:29
- 理由：事件本就全數流經此 callback，是既有的縫；orchestrator 同時跑在 ws.py 一般 session，對 autopilot 一無所知，此依賴方向必須守住
- 否決方案：在 orchestrator 新增顯式 `turn_started` 事件型別——會讓 orchestrator 耦合 autopilot 觀測需求，破壞依賴方向

## turn 起點由「事件帶入新 speaker」推斷，取 `tool_use.speaker_key` 或 `expert_message.speaker` 中先出現的新 speaker 為 `turn_started_at`，不新增 turn 事件型別
- 時間：2026-07-06 09:29
- 理由：用先到的工具或發言事件定起點，抵消「先靜默跑工具才發言」的延遲

## `speaker_key` 與 `expert_message.speaker` 進 tap 前先正規化成同一種 key，避免同一專家被判成兩個 turn
- 時間：2026-07-06 09:29

## turn state 用 `run_one_task` closure 內共享的 mutable holder，同時供 broadcast tap 更新與 `_task_heartbeat` 讀取，不落 journal、不用 module global、不加鎖
- 時間：2026-07-06 09:29
- 理由：holder 讀寫都在同一 event loop、await 邊界間無搶佔，無真並行，加鎖是多餘複雜度
- 否決方案：加 lock 保護 holder——asyncio 單執行緒模型下不需要，屬過度設計

## 事件驅動刷新只在 turn 邊界（新 speaker）、`tool_use`、`final=True` 的 `expert_message` 觸發 `_write_status`，跳過 streaming 逐塊事件
- 時間：2026-07-06 09:29

## 事件驅動寫入必須比照 `_task_heartbeat` 以 `_read_status` 帶回 `quota`/`sleep_until`/turn 欄位等既有欄位（對稱 preserve）
- 時間：2026-07-06 09:29
- 理由：高工核出的對稱漏洞——tap 觸發的 `_write_status("running",…)` 若不 preserve，會在任務中把主迴圈寫的 quota/sleep_until 閃成空，`/api/autopilot` 用量歸零

## `_write_status` 提供明確 preserve 參數或 helper，把「帶回既有欄位」收成單一入口，避免未來改 payload 時漏帶欄位
- 時間：2026-07-06 09:29

## 事件驅動寫入加最小寫入間隔節流（同 speaker 距上次事件驅動寫 <1–2s 即跳過，交由下個事件或下次 tick 補寫）
- 時間：2026-07-06 09:29
- 理由：高工核出 `tool_use` 非有界事件源，重工具迴圈一秒可噴多則，每則一次 atomic tmp+rename 會打爆原子寫；節流才守得住「寫入率有界」

## 事件驅動以 `time.time()` 蓋 `last_activity_at`；60s 背景 tick 完整保留（含 `events_mtime` 與 `workers.cpu_active` 盲區補償），兩軌並存不取代
- 時間：2026-07-06 09:29

## 60s 保底 tick 沿用 preserve 範式，從共享 state 帶回 `current_expert`/`turn_started_at`，避免每輪 tick 把 turn 欄位清 null
- 時間：2026-07-06 09:29

## turn 欄位在任務收尾清為 null，且清理需涵蓋 `_select_workflow`/clone 失敗提早 return 的路徑，防上一任務 `current_expert` 殘留；舊 status.json/舊 JSONL 無此欄位一律 null-safe 不回歸
- 時間：2026-07-06 09:29

## 監控判定維持「僅 `updated_at` 停滯（主迴圈死）**或**（`cpu_active==false` **且** `last_activity_at` 長不動）才殺」，AND 子句不放寬；`last_activity_at` 取代 journal 掃描僅作新鮮度來源，門檻 ≥3× 刷新間隔
- 時間：2026-07-06 09:29
- 理由：對應 2026-07-04 誤殺教訓——長工具靜默時 cpu_active 仍為 True 即不判死，兩訊號 AND 才殺

## 本任務與 `ttft_s`/prompt-caching 決策正交互不牽動；Claude path retry 去重缺口沿用共識只記 `核心改動: Claude provider 路徑缺乏 per-speak 去重保護` 進 backlog，本場不動 `experts.py`
- 時間：2026-07-06 09:29

## 零新工具零新腳本，重驗全用報告內既有指令（gh CLI + REST + `env PYTHONPATH=. python3 scripts/check_release_body_structure.py`），不自建勾稽自動化
- 時間：2026-07-06 19:14
- 理由：一次性重驗自證不了新抽象的複雜度；自動勾稽腳本會成為半年後沒人敢刪的死碼
- 否決方案：寫一支自動勾稽腳本供未來每版重驗——若真有此需求，另立專門任務

## `docs/evidence/*.json` 三檔凍結為不可變原始憑證，不覆寫、不新增 07-06 副本；今日結果只落報告「本次重驗」欄
- 時間：2026-07-06 19:14
- 理由：07-05 的 `captured_at_utc` 是擷取時刻的事實；雙份 evidence 會製造雙份真相，勾稽鏈反而斷裂
- 否決方案：新增 2026-07-06 evidence 檔並列存放

## `docs/release-e2e-closure-report.md` 為唯一可變產物，改動範圍限三列表重驗欄、第二章轉錄（逐字貼 2026-07-06 輸出並標註日期）、缺口章
- 時間：2026-07-06 19:14
- 否決方案：沿用 07-05 舊輸出僅加註——驗收標準 2 明文要求本次實際輸出

## 雜湊規則章與正規化規則凍結，報告端零衍生雜湊，只引 evidence 既有值
- 時間：2026-07-06 19:14

## 逐欄比對一律指令化：以 `jq` 從 $TMPDIR 原始輸出抽欄位、與 evidence 值 `diff`，報告重驗欄附上抽取指令；不接受「用眼比對」作為 match 依據
- 時間：2026-07-06 19:14
- 理由：眼比出的 match 無法自證，違反「實跑行為，不靠看起來對」的既有教訓（採高工意見）
- 否決方案：人工目視逐欄核對

## #1/#2 動工前先落定欄位分類——身分欄位（tagName、body 雜湊來源值、run_id、event、status、conclusion、workflow_path）不符即缺口；易變欄位（`updatedAt`、下載計數等合法漂移欄位）只記錄不比對
- 時間：2026-07-06 19:14
- 理由：不先定分類，執行者現場自由裁量會讓缺口章公信力打折（採高工意見）
- 否決方案：執行時遇到再判斷

## 重驗原始輸出一律存 `$TMPDIR` 且檔名帶 task 編號等唯一識別，禁落 docs/，#1/#2 並行互不踩檔（採工程師意見）
- 時間：2026-07-06 19:14

## mismatch 路徑＝如實寫缺口章＋結論降級，不現場修復、不動 evidence、不動線上資源；全 match＝結論明文限定「閉環（僅及 v0.2.0）」
- 時間：2026-07-06 19:14
- 理由：修復是新任務，本場只負責如實記錄，先定失敗路徑擋範圍爆炸

## `path` 欄位維持 N/A＋REST 補驗雙落字，判定值取 REST 的 `workflow_path`
- 時間：2026-07-06 19:14

## 收尾驗收精準化：`git diff docs/` 只允許 `docs/release-e2e-closure-report.md` 有預期改動，`docs/` 無任何 untracked；非「整個 docs/ 乾淨」（採工程師意見）
- 時間：2026-07-06 19:14

## 第二章轉錄更新時不得動到 marker 行與守護測試 grep 的報告字串；#4 驗收含 `.venv/bin/python -m pytest tests/docs -q` 全綠，報告內指令全維持 `python3`/`.venv/bin/python`
- 時間：2026-07-06 19:14

## `gh` 認證、網路可達、線上 v0.2.0 release/run 27905531397 仍可讀列為重驗前置條件，#1/#2 起手先驗；前置不成立即回報阻塞，不以舊值充當重驗結果
- 時間：2026-07-06 19:14

## 唯一權威決議檔落 `docs/task3-authoritative-decision-2026-07-08.md`，沿用既有 `release-e2e-authoritative-declaration` 的 additive 宣告範式，不覆寫既有 evidence/closure/handoff
- 時間：2026-07-08 06:57
- 理由：既有範式已被 `tests/docs/` 守門驗證過，讀者心智模型與測試比照方式一致
- 否決方案：另立新格式——會讓守門測試無從比照且範圍爆炸

## 作廢標記採 ADR「Superseded by」語義，五份原檔 immutability——存在則不刪除、不改寫歷史
- 時間：2026-07-08 06:57

## 因五份採固定五列占位契約，僅 `778ced`/`rerun-765f1b` 為 QA 訊息流已明列 task3 短碼；未明列欄位固定填 `<訊息流未明列>`，路徑欄固定填 `訊息流明列值／查無 repo 實體檔`
- 時間：2026-07-08 06:57
- 理由：硬追形式雙向會逼我們憑空造五個檔或改歷史，兩者都違反 immutability；且不講白會讓終判卡在「作廢對象查無此檔」
- 否決方案：憑空造五個原檔以湊齊雙向連結

## 固定五列中已明列的識別碼與省略 hash 逐字照抄 QA 訊息流，省略號（U+2026）原樣保留，落盤與測試斷言一律用 bytes 比對，防編輯器/格式化把 `…` 正規化成 `...`
- 時間：2026-07-08 06:57

## 帶省略號的 `99f330…9d3b` 類值只當「訊息流逐字值」，不得測成真實 64 碼 sha256
- 時間：2026-07-08 06:57
- 理由：它是自證用短碼、非外部整檔校驗值，用 64-hex 正則會誤紅或漏抓

## 落盤用標準庫原子寫入（temp→flush→`os.fsync`→`os.replace`），不新增第三方依賴
- 時間：2026-07-08 06:57
- 否決方案：引入 `atomicwrites` 第三方套件——標準庫已足夠且違反不加依賴慣例

## 落盤腳本一次性、走 `$TMPDIR`，不留在 repo 被掃描目錄，收尾以 `git status docs/ tests/` 確認無 untracked 殘留
- 時間：2026-07-08 06:57
- 理由：被掃描目錄的 untracked 臨時檔會造成 pre-commit 綠／CI 紅分歧

## 權威檔內硬分「整檔 sha256（外部權威）」與「檔內嵌 hash（僅自證）」兩節，附 `python3` 自驗指令
- 時間：2026-07-08 06:57
- 理由：前一輪 `c2f4bb→725cf1` 重跑的踩坑根因即語義未分，不可省

## `tests/docs/` 新守門測試不繼承 task2 的 `SHA256_RE`（64-hex）＋evidence 檔比對邏輯，改抓「必要標題＋五個識別碼逐字存在＋省略號原樣＋`Superseded by` 語義＋非實體檔標註＋回報路徑字串可讀＋報告內無裸 python」
- 時間：2026-07-08 06:57
- 理由：本任務值為短碼/帶省略號且 evidence 實體檔不存在，沿用 64-hex 掃描會漏抓或誤紅
- 否決方案：照抄 `test_release_e2e_closure_report_task2.py`

## 本任務邊界限定 `docs/` 一份新檔 + `tests/docs/` 一支守門測試，零 `studio/` 主套件改動
- 時間：2026-07-08 06:57

## 回報值＝權威檔相對路徑字串 `docs/task3-authoritative-decision-2026-07-08.md`，由守門測試斷言可程式讀出
- 時間：2026-07-08 06:57
## 認證改用 git config env 注入（`GIT_CONFIG_COUNT=1`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`）帶 base64 extraHeader
- 時間：2026-07-08 16:21
- 理由：「token 不進 argv/ps」是本任務安全本質，唯一能真正成立的做法；label 只遮 RunOutput.command，遮不掉 ps 短窗
- 否決方案：inline `-c http.extraHeader=`——其值本身即 argv 元素，一樣進 argv/ps 不合格

## `run_command_exec` 新增 `env: dict|None=None` 參數，僅 `env is not None` 時合併 `{**os.environ, **env}` 後傳入
- 時間：2026-07-08 16:21
- 理由：create_subprocess_exec 的 env 會取代整包環境，必須 merge os.environ；預設 None 維持繼承、向後相容，且是可複用的通用能力
- 否決方案：用 `os.environ` 全域改寫——多 session 併發下互相污染，是不可逆隱性 bug；亦否決預設 `env={}` 清掉繼承

## `remote_url(repo)` 移除 token 參數、回乾淨裸 URL `https://github.com/<repo>.git`，保留嚴格 repo 格式校驗與 `assert_repo_allowed` chokepoint
- 時間：2026-07-08 16:21
- 理由：乾淨 URL 不含 token；allowlist + 格式校驗防 repo 字串被拿去組惡意 URL；blast radius 僅 publisher 兩處 + 3 測試呼叫

## 簽名破壞 `remote_url(repo, token)→remote_url(repo)` 連帶改 `test_publisher.py:48`、`test_owner_allowlist.py:113/119`，owner-allowlist 反向黑樣本（otherowner）必須保留
- 時間：2026-07-08 16:21
- 理由：反向黑樣本證明 chokepoint 沒隨簽名變動被弱化，防假綠

## 新增純函式 `extra_header(token)` 與 `git_auth_env(token)`，base64 一律 `b64encode(f"x-access-token:{token}".encode()).decode()`（無尾換行），per-host key `http.https://github.com/.extraheader` 收斂作用域
- 時間：2026-07-08 16:21
- 理由：per-host key 對齊 actions/checkout，避免 header 隨重導向送到其他 host；純函式可單測釘死尾換行坑

## `redact` 擴充遮 base64 形式，且必須用與 `git_auth_env` 完全相同的編碼（相同前綴、無尾換行）算出同一字串再 replace
- 時間：2026-07-08 16:21
- 理由：編碼不一致＝遮了對不上的值＝假遮罩；需補測試斷言 redact 真的把該 b64 換掉

## `_push`/`_push_base` 新增 `env` 透傳；fake_push／fake_push_base 替身統一用 `**kwargs` 收尾
- 時間：2026-07-08 16:21
- 理由：`**kwargs` 讓 6+ 檔測試替身對簽名擴充免疫，避免逐檔漏改造成 CI 綠/紅分歧

## 守門測試釘兩件事——argv/command 不含 token 明文與 base64；`GIT_CONFIG_VALUE_0` 含正確無換行 base64 header，並斷言 env 確實到達子行程
- 時間：2026-07-08 16:21
- 理由：拆兩點才能同時證明「URL/argv 不洩密」與「認證有效」

## runner.py 與 publisher.py 均屬 Ti 核心，走 `x812033727/Ti` 核心 repo 獨立 PR，不混進專案 repo
- 時間：2026-07-08 16:21

## 移交待辦（不在本輪）——`GIT_CONFIG_COUNT` 需 git 2.31+，於註解寫一行最低版本假設；runner env 參數於未來 `sandbox=True` 場景時，bwrap 是否 `--clearenv` 吞掉 env 須另案確認
- 時間：2026-07-08 16:21
- 理由：本任務兩處 push 均 `sandbox=False`，不觸發 bwrap env 轉發問題，列為註記/待辦不阻擋本輪

## lane baseline 注入契約：env/manifest 優先序與 fail-open/closed 分流策略
- 時間：2026-07-09 01:40
- 決策：並行 lane（`LaneContext` + git worktree）啟動設定基準（env + manifest）的注入層落地時，優先序固定為 `顯式注入 > env(TI_*) > lane manifest > 模組 DEFAULT`（env 覆蓋檔案，與 `config.py`/`settings.py` 同源）；缺失/非法時按注入項性質分流——安全/正確性關鍵項 fail-closed 中止該 lane，非關鍵增益項 fail-open 退回 `DEFAULT` + `log.warning`。契約細節與決策表見 ARCHITECTURE.md『任務並行』節「baseline 注入契約」子段。此 baseline 與 `runner.write_baseline_gitignore`（發佈前 .gitignore 淨化）同名不同源。
- 狀態：前瞻契約，lane 注入層尚未落地；決策表所列 `AUTOPILOT_REPO`（fail-closed）、`TI_DISCUSS_MODE`（fail-open）為準則類比佐證，非現有 lane 注入行為。
- 移交待辦：`lane 注入層落地後補守門測試對齊決策表`。
- 理由：對齊 repo 既有 SSOT（env 覆蓋檔案、顯式注入優先），避免另立 lane 專屬表述造成文件漂移；fail 策略依失效後果分流而非一刀切。
- 否決方案：env/manifest 一刀切同一 fail 策略；為 lane 另立獨立優先序；於 ARCHITECTURE 另開獨立章節而非內嵌子段。
## 技術選型採純 bash 腳本、零新依賴（gitleaks 僅 `--no-git` 可選、grep fallback 為主軸）
- 時間：2026-07-10 16:31
- 理由：驗證/掃描本質是 gh/curl/grep 的 shell 動作，bash 最貼合；此腳本不進 orchestrator 資料流、不 import studio，是獨立運維工具
- 否決方案：Python wrapper（雖與 repo 主語言一致，但只多一層 subprocess 表面，一致性收益低於直接性）

## 腳本切為 `--verify`/`--scan`/`--report` 三個互不耦合子命令，各自可獨立執行、無共享狀態
- 時間：2026-07-10 16:31
- 理由：三段執行前提不同——`--verify` 需人在場有 `$GH_PAT`，`--scan`/`--report` 無 token 也能跑；解耦讓 #4 在無 token 時仍能完成
- 否決方案：一鍵全跑組合模式（會逼無 token 的 #4 卡在 `--verify`）

## 掃描目錄參數化（預設 `history/`、workspace-dir 由參數傳入），不寫死絕對路徑
- 時間：2026-07-10 16:31

## 依賴方向固定為單向「腳本 → runbook」——腳本不內嵌四項 PAT 規格文字，僅在 `--report` 輸出「請人工核對 runbook 四項規格」指引
- 時間：2026-07-10 16:31
- 理由：內嵌規格會製造第二份 SSOT，runbook 改動後腳本漏改即漂移
- 否決方案：腳本自帶完整規格說明（看似方便，實則雙來源）

## token 明文資料流為單向流入、永不流出——腳本內零明文輸出路徑，禁用 `set -x`、`curl -H` 不得被 log 印出，可 grep 自證，守門測試鎖此不變式
- 時間：2026-07-10 16:31

## 守門測試置於 `tests/docs/test_qa_token_rotation_script.py`，以字串錨鎖定（`GH_TOKEN=` 綁定、curl fallback、全前綴 regex、無裸跑 `gh auth status`）
- 時間：2026-07-10 16:31

## 字串錨須精準鎖「可執行行」，排除註解/heredoc/`--report` 指引文字中的 `gh auth status` 說明範例，避免自傷誤觸
- 時間：2026-07-10 16:31
- 理由：高工指出 `--report` 說明文字含 `gh auth status` 字樣會誤觸「須帶 GH_TOKEN 前綴」錨
- 否決方案：全文粗鎖 `gh auth status`（會把說明範例當違規）

## 不使用 AST 鎖，沿用字串錨——這是 shell 非 Python，同源於既有「示例順序不上 AST」的可逆性理由
- 時間：2026-07-10 16:31

## 守門測試絕不對 repo `history/` 實跑；黑/白樣本一律在 `$TMPDIR` 自建掃描目標傳入 `--scan`，只驗判別力、不依賴 repo 狀態
- 時間：2026-07-10 16:31
- 理由：repo `history/` 已有大量真實 `pjd*.jsonl`，對它實跑會讓測試隨 log 內容脆化、變慢（教訓庫「臨時檔不落被掃目錄」翻版）

## 「對真實 repo `history/` 實跑一次殘留掃描」列為 #4 的證據項，結果併入 `--report` 唯讀摘要呈現，不靠守門測試順帶掃
- 時間：2026-07-10 16:31
- 理由：session 事件存檔殘留 token 屬真陽性安全發現，非誤報；與守門測試的「只驗判別力」須徹底分開

## exit code 契約——`--report` 恆 0、`--scan` 命中殘留回非 0、`--verify` 依驗證結果；`--scan` 命中須併入 `--report` 摘要，不得靜默
- 時間：2026-07-10 16:31
- 理由：本輪不接 CI，`history/` 若有既存命中須靠 `--report` 被看見（無 silent 截斷）

## 本輪不新增 CI gate、不動 `ci.yml`——守門測試落 `tests/docs`，既有 test job 自動涵蓋
- 時間：2026-07-10 16:31

## 本輪 #4 僅執行 `--scan`/`--report` 並回填證據，`--verify` 與步驟 1（發新）、步驟 3（撤舊）明確標示待人工於 GitHub UI 完成
- 時間：2026-07-10 16:31

## **修法選 `+` force refspec**：三處 fetch argv 均改為 `["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]`
- 時間：2026-07-10 23:00
- 理由：`+` 前綴精確跳過目標 ref 的 CAS，不影響其他 ref；deploy_dir 為 origin 單向鏡像，force 覆蓋 remote-tracking ref 是預期行為
- 否決方案：`--force` flag（語意過寬，未來新增 refspec 行為不確定）；重試保險層（根修後多餘，隱藏同類 bug）；架構重構（消除鎖外 fetch，改動面大、非此輪範圍）

## **修改範圍鎖定三處**：`studio/autodeploy.py:60`、`studio/deploy.py:159`、`studio/autopilot.py:2324`
- 時間：2026-07-10 23:00
- 理由：三者均以 deploy_dir 為 cwd，fetch 後即讀/reset `origin/<branch>`，存在多寫者 CAS 競爭，修法統一不設例外

## **明確排除 `repo_base.py:143`**
- 時間：2026-07-10 23:00
- 理由：該行 fetch 只寫 FETCH_HEAD，不寫 `refs/remotes/origin/*`，CAS 不適用

## **明確排除 `autopilot.py:120`**
- 時間：2026-07-10 23:00
- 理由：autopilot work clone 為單寫者，不存在並發競爭；加 force refspec 無害但掩蓋「此處走不同路徑」的資訊

## **守門測試採 argv 捕捉（monkeypatch `_run`），不實跑 git**
- 時間：2026-07-10 23:00
- 理由：驗收閉環禁止對 `/opt/ti` 打真實 fetch（會重現 bug 現場）；字串錨 `+refs/heads/` 鎖定 argv 形式

## **測試必須逐一覆蓋三個 call site，不得以單處代表全部**
- 時間：2026-07-10 23:00
- 理由：高工指出「黑樣本 FAIL 只證錨字串有效，不證三處都改到」——漏改一處、單一測試仍可能綠；三處需各自捕捉 argv 驗 `+refs/heads/`

## **force 語意須在 commit message 或行內加註**：`deploy_dir 單向鏡像，force 更新 remote-tracking ref 為預期行為`
- 時間：2026-07-10 23:00
- 理由：`+` 前綴半年後接手者會疑惑「是否吞本地 commit」，隱含前提須文字化，防止後人「安全起見拿掉」引發 regression

## **測試檔位置：`tests/deploy/test_fetch_force_refspec.py`**，三處 call site 同一檔驗收
- 時間：2026-07-10 23:00
- 理由：與既有 deploy 守門測試同目錄，CI `tests/deploy` job 自動涵蓋；三處同一 argv 契約，集中管理

## **diff 邊界鐵則：五個檔**——`studio/autodeploy.py`、`studio/deploy.py`、`studio/autopilot.py`、`tests/deploy/test_fetch_force_refspec.py`、`tests/test_task1_retry_doc.py`；超出須另立任務
- 時間：2026-07-10 23:00（2026-07-11 修正：四檔 → 五檔）
- 修正理由：原宣稱「僅四個檔」，但實際 diff 含第五檔 `tests/test_task1_retry_doc.py`——這是**必要的伴隨護欄修正**，非鍍金污染。`SCOPE_GUARD_MAINTENANCE_GLOBS` 白名單只含 `tests/_scope_guard.py|conftest.py|test_task1_retry_doc.py`，**不含 `studio/*.py`**；舊版 `test_no_py_changed` 護欄會對本 lane 三個 studio `.py` 改動判 FAIL、CI 紅。修正將護欄從「看 lane 編號」改為「看是否實際改動 `ARCHITECTURE.md`」（`if "ARCHITECTURE.md" not in changed_files: skip`），並把 API 由 `find_repo_scope_violations` 換成 `collect_changed_files`+`find_scope_violations`。實測保留版該護欄為 SKIPPED，不再誤觸發擋死 #1 合法改動。
- 移交：護欄以 lane 編號當任務身分屬 Ti 核心測試基建設計缺陷，另立 `核心改動:` 任務根治（見後續任務）。

## 所有 prefilter 邏輯集中於 `studio/autopilot.py`，不新增模組
- 時間：2026-07-10 21:15
- 理由：prefilter 是 pick 前同步判定，與 investigation lane、`_token_set_similarity`、`_tokenize_for_dedup` 同倉庫，跨模組界面為零
- 否決方案：放 `flow.py`——flow.py 契約為無狀態純函式，async 取 merged 標題無法放入

## 新增 `async def _fetch_merged_titles(clone: str, repo: str, since_days: int) -> list[str]`
- 時間：2026-07-10 21:15
- 理由：非同步才不阻塞 event loop，且 git fallback 需要 clone 路徑
- 否決方案：同步版本（阻塞）；透過 `publisher` 模組（會造成循環 import）

## 主路徑用 lazy `import httpx`，呼叫 `GET /repos/{owner}/{repo}/pulls?state=closed&per_page=100`，過濾 `merged_at != null` 且在 lookback 窗內
- 時間：2026-07-10 21:15
- 理由：httpx 已為現有依賴（`publisher.py` 慣例），lazy import 避免常駐記憶體
- 否決方案：引入 PyGithub（新依賴，不值）

## git fallback 用 `git log --format=%B%x00 --since=<n>.days.ago`，以 `\x00` 切割各 commit body，取每則 body 第一個非 merge-subject 的有效行作標題
- 時間：2026-07-10 21:15
- 理由：`--oneline` 下 GitHub merge commit subject 常是 `Merge pull request #…`，PR title 在 body 第一行；用 `%B%x00` 才能取到有效語意
- 否決方案：`--merges --oneline`（常返回 merge subject 而非 PR title，語料雜訊大）

## git fallback 在 shallow clone 或無歷史時回傳空 list，靜默放行（任務不降級）；在函式 docstring 明確標 known-limitation
- 時間：2026-07-10 21:15
- 理由：偏誤方向安全（漏判優於誤殺）；補測試「shallow clone 下 fallback 回空、任務不降級」

## 模組級快取 `_MERGED_TITLE_CACHE: dict[tuple[str, int], tuple[float, list[str]]]`，key 為 `(repo, since_days)`，TTL 3600 秒
- 時間：2026-07-10 21:15
- 理由：型別具體、IDE 可靜態檢查；一 loop 內多任務共用同批 merged 標題，不重複打 API
- 否決方案：`dict[str, ...]`（key 型別過鬆，工程師指正）

## cache key 不含 token 狀態；同一 loop 內 token 穩定，影響可忽略；加一行注釋說明此假設
- 時間：2026-07-10 21:15

## httpx 僅取第一頁 `per_page=100`；活躍 repo 60 天 merged PR 可能 >100，漏舊 PR 導致漏判（放行，方向安全）；在函式 docstring 標 known-limitation，不分頁
- 時間：2026-07-10 21:15
- 否決方案：分頁全取（複雜度高，漏判代價低，不值）

## 新增 3 個 config 旋鈕，於 `config.py` 頂層與 `reload()` 區塊兩處同步定義
- 時間：2026-07-10 21:15

## `AUTOPILOT_PREFILTER_RATIO` 獨立於 `AUTOPILOT_DEDUP_RATIO`（0.75），預設 0.80
- 時間：2026-07-10 21:15
- 理由：prefilter 誤殺代價（合法任務多一場 investigation + 一輪 pick 延遲）高於漏判代價；門檻拉高壓窄命中面
- 否決方案：共用 DEDUP_RATIO（語意不同，後續分別調整才有意義）

## 插入點為 `run_one_task` 中 `await _prepare_clone()` 之後、`_is_investigation_task()` 之前
- 時間：2026-07-10 21:15
- 理由：clone 路徑是 git fallback 必需；先於 investigation 路由才能接管降級決定

## `backlog.annotate` 具名新增 `lane: str | None = None` 參數，僅 `lane is not None` 時寫入；函式內部 **不得** 接受 `**extra_fields`
- 時間：2026-07-10 21:15
- 理由：`annotate` 契約是「只補 note，不動 status/attempts」；裸 `t.update(**extra_fields)` 是後門，任何呼叫端傳 `status`/`attempts`/`id` 可靜默覆蓋關鍵欄位，擊穿保護語意
- 否決方案：`**extra_fields` 無白名單擴展（高級工程師退回，最便宜時機是設計階段修）

## 命中時呼叫 `backlog.annotate(task_id, note="[prefilter-implemented] 疑似已實作，匹配 merged: {title}", lane="prefilter-implemented")`，再路由 `_run_investigation_task`；命中不得靜默
- 時間：2026-07-10 21:15

## anti-ping-pong 沿用既有機制：`lane="full"` 時跳過 prefilter；investigation 判「需改碼:」→ 退回 `pending + lane="full"` → 下輪走全管線不再降級
- 時間：2026-07-10 21:15
- 否決方案：新增獨立 `lane="prefilter-skip"` 旗標（重複語意）

## 低資訊保護：`len(_tokenize_for_dedup(title)) < 3` 時跳過比對，不降級
- 時間：2026-07-10 21:15
- 否決方案：`len(title.split()) < 3`（CJK 不靠空格分詞，分詞已有現成函式）

## `VALID_TYPES` 維持不變，不新增 `"verification"` 型別；降級走既有 investigation lane
- 時間：2026-07-10 21:15
- 否決方案：新增型別（需動 backlog 驗證 + 所有消費端，複雜度不值可讀性收益）

## 零新依賴，禁 `rapidfuzz`；相似度一律複用 `_token_set_similarity`（詞集 Jaccard，CJK 已處理）
- 時間：2026-07-10 21:15
- 否決方案：rapidfuzz（現有輪子在短標題場景等效，加依賴增部署與審查成本）

## #3 測試必含以下五個黑白樣本：① 命中 → 降級為 investigation；② 未命中 → 任務不動；③ token < 3 → 不誤殺；④ 總開關關閉 → 整段旁路；⑤ 無 GH token / shallow clone → fallback 回空、不炸不誤殺
- 時間：2026-07-10 21:15
## `make_env(token: str | None, url: str | None = None) -> dict[str, str]`；url 提供且 host 非 `github.com` 時回空 dict；url 為 None 時仍產生 github.com per-host env（key 已釘死 host，不跨域）
- 時間：2026-07-11 03:03
- 理由：集中 host 判斷於 SSOT，caller 不重複；url=None 安全，因 key 本身限定 `http.https://github.com/`
- 否決方案：caller 自行判斷再決定是否呼叫 make_env（責任分散，易漏）

## autopilot `_GIT_CRED` 替換必須附 `realgit` marker 的 clone/fetch 閉環測試，驗證 `config.GITHUB_TOKEN` 對 CORE_REPO 可通；push env dict 透傳由單元測試 assert（mock push）；`config.py` 文件明載「`GITHUB_TOKEN` 須持有 `AUTOPILOT_REPO` write 權限」
- 時間：2026-07-11 03:03
- 理由：gh CLI helper 走 gh 憑證、env token 走 PAT，兩者 scope 不等價，不能只靠 assert dict 內容
- 否決方案：只靠單元測試斷言 env dict 結構（無法驗證 CORE_REPO 真實認證可用）

## `scrub_remote(repo_path)` 呼叫點固定為兩處 — ①`runner.git_clone` 成功後（legacy=False）；②`repo_base.sync_workspace` fetch 成功後（legacy=False）；不做一次性 migration 工具，不在 import 或 config reload 時觸發
- 時間：2026-07-11 03:03
- 理由：接點對應「token 可能寫入 .git/config 的操作」，精確貼緊污染來源
- 否決方案：caller 自行決定觸發時機（分散易漏）；一次性 migration（legacy 情境可能持續出現）

## git 版本偵測採 module 層級 `_GIT_ENV_SUPPORTED: bool | None = None` 快取；首次呼叫 `make_env` 時 lazy 偵測，不在 import 時做 subprocess
- 時間：2026-07-11 03:03
- 理由：import 時做 subprocess 是隱藏副作用；無 git 環境（CI collect-only）會炸；lazy 讓測試可 monkeypatch
- 否決方案：module import 時立即偵測（副作用難控、import 期異常難追）

## `build_clone_url(url, token, *, legacy: bool) -> str`，legacy 為顯式必填關鍵字參數；caller（`runner.git_clone`）自行讀 `config.TI_GIT_CRED_LEGACY` 後傳入
- 時間：2026-07-11 03:03
- 理由：純函式不讀 config，可在任何 config 狀態下直接單元測試
- 否決方案：函式內部讀 `config.TI_GIT_CRED_LEGACY`（破壞可測性，引入隱式 config 依賴）

## `make_env` 固定使用索引 0；文件明載「假設父環境無 `GIT_CONFIG_*`」；caller 以 `{**os.environ, **make_env(token)}` merge；#2 測試補一條「父環境已帶 `GIT_CONFIG_COUNT=1` 時 make_env 覆蓋 KEY_0/VALUE_0」的行為樣本
- 時間：2026-07-11 03:03
- 理由：動態累加父環境 COUNT 引入 os.environ 讀取副作用，複雜且難測；Ti 執行環境父 env 無 GIT_CONFIG_* 可接受此假設
- 否決方案：動態讀父環境 COUNT 再累加（stateful、測試難、副作用）

## `git_cred_argv` 回傳的 `-c http.extraHeader=...` 所有呼叫點的 label 必須是固定短字串；#2/#3 測試 assert `RunOutput.command` 不含 base64 encoded token
- 時間：2026-07-11 03:03
- 理由：RunOutput.command 進 history/broadcast，base64 token 等同明文洩漏

## `git_cred.py` 包含 `_auth_b64`（私有）與 `make_env`、`clean_url`、`git_cred_argv`（公開）；`publisher.git_auth_env` 改委派 `git_cred.make_env`；`publisher.redact` 改 import `git_cred._auth_b64` 以維持 base64 遮蔽邏輯一致
- 時間：2026-07-11 03:03
- 理由：編碼邏輯集中於 SSOT 不重複；publisher 只負責 redact 語義，不重算 base64
- 否決方案：publisher 保留自己的 `_auth_b64` 副本（兩份邏輯若日後修改會分叉）；git_cred import publisher（循環依賴風險）

## `config.py` 的 `TI_GIT_CRED_LEGACY` 定義旁加 comment `# 下線判準：連續兩個 minor release 無 legacy=True 生產回報 → 刪閥`；不設固定日期
- 時間：2026-07-11 03:03
- 理由：以版本為單位比日期更貼近實際 review 時機；comment 記錄判準避免死碼化
- 否決方案：不設任何判準（legacy 永久殘留）；固定日期（到期無人看等於沒設）

## `repo_base._redact` 與 `publisher.redact` 均保留，各自防守自身輸出路徑，不合併
- 時間：2026-07-11 03:03

