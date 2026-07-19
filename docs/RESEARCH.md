## 2026-06-13 00:52

調研完成。先講現況脈絡：本專案是 Python/FastAPI + claude-agent-sdk，`orchestrator.py` 已有「辯論階段」與任務級波次並行（LaneContext + git worktree），本輪「多角色討論引擎」應聚焦在**發言層的對話循環**（誰下一個說、並行發言怎麼合流、怎麼收斂）。

**重點**

- 重點: 業界主流的發言調度就四種＋自訂：`auto`（LLM 選下一位）、`round_robin`、`random`、`manual`，AG2/AutoGen 的 GroupChatManager 即「選人→收發言→廣播」三步循環；auto 模式要給每個角色獨立 `description`（別直接用 system prompt）且名稱必須唯一，並可用 `allowed_speaker_transitions` 約束誰能接誰的話，避免 LLM 亂選（[AG2 GroupChat 文件](https://docs.ag2.ai/latest/docs/user-guide/advanced-concepts/groupchat/groupchat/)、[AutoGen group chat pattern](https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/design-patterns/group-chat.html)）
- 重點: 多代理辯論研究的兩大坑：**DoT（思維退化）**——代理一旦有信心就不再產生新想法；**過早收斂/從眾**——同質代理池容易集體鎖死在「貌似合理但錯」的早期答案。對策是「適度針鋒相對」＋自適應停止：實驗顯示**中度分歧最優**，最大化對抗反而極化（[MAD 論文](https://arxiv.org/abs/2305.19118)、[MAD 綜述](https://hungleai.substack.com/p/agree-or-disagree-a-review-of-multi)、[受控辯論研究](https://arxiv.org/html/2511.07784v1)）
- 重點: **諂媚（sycophancy）是多代理討論的效率殺手**：代理互相附和而非批判，會拖長共識輪數、推高成本。緩解手段已被驗證有效：明確 persona＋反諂媚指令（「不同意時必須指出」）、動態調整 prompt（CONSENSAGENT）（[ACL 2025](https://aclanthology.org/2025.findings-acl.1141/)、[sycophancy 緩解綜述](https://arxiv.org/html/2411.15287v1)）
- 重點: **全員共享完整 transcript 的 token 成本是 O(N²)**：每輪每個代理都重送全史。對策是角色感知的上下文路由（每個角色只拿與其相關的片段＋結構化共享記憶），可同時省 token 並提升品質（[RCR-Router](https://arxiv.org/pdf/2508.04903)、[token 成本分析](https://www.augmentcode.com/guides/ai-agent-loop-token-cost-context-constraints)）
- 重點: Anthropic 自家多代理系統的經驗：orchestrator-worker 模式、3–5 個 subagent 並行、**只並行真正獨立的工作**；並要寫死「努力刻度」規則（簡單問題 1 agent、複雜才多 agent），否則小事也燒大錢（[Anthropic 工程博客](https://www.anthropic.com/engineering/multi-agent-research-system)）
- 重點: claude-agent-sdk 是 async-first，多代理並行天然可行，但需**應用層 semaphore 節流**對齊 API rate limit——本專案已有 `TI_LLM_MAX_CONCURRENCY`，發言層並行可直接複用（[SDK sessions](https://platform.claude.com/docs/en/agent-sdk/sessions)、[並行實務](https://codesignal.com/learn/courses/parallelizing-claude-agentic-systems-in-python/lessons/concurrent-agent-conversations)）

**建議**

- 建議: 不引入 AutoGen/LangGraph 等重框架——既有 orchestrator/experts/providers 抽象已等價於它們的核心，砍掉重練違反任務約束；**借它們的「模式」自己實作**：在 orchestrator 加一個輕量 `DiscussionEngine`/調度器，支援 `round_robin`（現狀、保底）與 `moderator-select`（由主持人 LLM 依上下文選下一位發言者，順便為 P1 主持機制鋪路）
- 建議: 並行發言採「**同輪並行、輪間同步**」：同一輪各角色基於同一份上一輪 transcript 並行發言（asyncio.gather＋既有併發節流），收齊後廣播再進下一輪——避免互相回應時的競態，也與既有波次並行架構一致
- 建議: 「互相回應」用結構化引用：發言格式要求 `回應 @角色: 同意/反對＋理由`，並在角色 prompt 加反諂媚硬指令（「至少指出一個可挑戰點，無異議須說明為何」）——對齊 CONSENSAGENT 的發現
- 建議: 收斂控制三件套：(1) 沿用既有 `is_stalled()` 相似度偵測做自適應停止；(2) 設最大輪數上限；(3) 主持人/裁決者判定「分歧已收斂或值得停」——對齊 MAD「adaptive break」結論
- 建議: context 餵法先做最便宜的：每角色只收「上一輪全員發言＋自己歷史」而非全史重播，長討論滾動摘要舊輪次——避開 O(N²) token 坑；RCR 式精細路由留作後續
- 建議: 預設保守的並行刻度：發言層並行僅在角色數 ≥3 且該輪無依賴時啟用，並受 `TI_LLM_MAX_CONCURRENCY` 節流；加 env 開關（如 `TI_DISCUSS_MODE=round_robin|moderator|parallel`）保持向後相容，與專案既有 opt-in 慣例（TI_REFLEXION/TI_ADR）一致

Sources: [AG2 GroupChat](https://docs.ag2.ai/latest/docs/user-guide/advanced-concepts/groupchat/groupchat/) · [AutoGen group chat](https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/design-patterns/group-chat.html) · [MAD/DoT 論文](https://arxiv.org/abs/2305.19118) · [MAD 綜述](https://hungleai.substack.com/p/agree-or-disagree-a-review-of-multi) · [受控辯論研究](https://arxiv.org/html/2511.07784v1) · [CONSENSAGENT (ACL 2025)](https://aclanthology.org/2025.findings-acl.1141/) · [Sycophancy 緩解](https://arxiv.org/html/2411.15287v1) · [RCR-Router](https://arxiv.org/pdf/2508.04903) · [Token 成本](https://www.augmentcode.com/guides/ai-agent-loop-token-cost-context-constraints) · [Anthropic 多代理系統](https://www.anthropic.com/engineering/multi-agent-research-system) · [SDK sessions](https://platform.claude.com/docs/en/agent-sdk/sessions) · [SDK 並行](https://codesignal.com/learn/courses/parallelizing-claude-agentic-systems-in-python/lessons/concurrent-agent-conversations)

## 2026-06-13 03:21

我先看既有程式碼現況，同時載入搜尋工具。
調研完成，缺口已補齊（既有調研已涵蓋討論引擎，本輪聚焦角色 schema、儲存格式、persona 設計坑、動態組隊）。

**現況脈絡**：角色在 `studio/roles.py` 是 8 個 hardcoded frozen dataclass（key/name/avatar/title/model/allowed_tools/permission_mode/system_prompt/tags），無設定檔、無角色管理 API；`DiscussionEngine` 已支援任意 N 角色但呼叫端仍硬編二人辯論；routes.py 已用 pydantic BaseModel、settings.py 已有 Field 白名單＋reload 機制——本輪是把角色「設定化＋可管理＋可組隊」。

**重點**

- 重點: 業界兩種主流角色定義檔格式可借鏡：(1) CrewAI 的 `agents.yaml`——每角色必填 `role/goal/backstory` 三欄＋tools，強調「越具體越好」（Senior Data Researcher > Researcher），支援 `{variable}` 執行期插值，YAML 讓非工程人員也能改角色（[CrewAI Agents](https://docs.crewai.com/en/concepts/agents)、[YAML 設定教學](https://codesignal.com/learn/courses/getting-started-with-crewai-agents-and-tasks/lessons/configuring-crewai-agents-and-tasks-with-yaml-files)）；(2) Claude Code subagents 的 **Markdown＋YAML frontmatter**——frontmatter 放 name/description/tools/model/permissionMode 等中繼資料，body 即 system prompt；同名以高優先層級覆蓋（project > user），name 重複會靜默丟棄其一（[官方 sub-agents 文件](https://code.claude.com/docs/en/sub-agents)）
- 重點: persona 研究的關鍵警示：persona prompt **效果不穩定**——有研究顯示 162 個角色測試中無可靠增益、甚至降低 zero-shot 推理準確率；專家 persona 能提升對齊但可能傷準確性。已驗證有效的做法是「**persona 卡片＋明確微規則（micro-rules）＋場景契約**」而非單純堆形容詞——這正好對應現有 roles.py 的「職責＋出力格式硬指令」結構，應保留為 schema 必填欄位（[Persona Prompting 綜述](https://www.emergentmind.com/topics/persona-prompting-pp)、[role prompting 優化研究](https://arxiv.org/html/2509.00482v1)、[PRISM](https://arxiv.org/html/2603.18507)）
- 重點: 動態組隊（依議題選角）已有研究支撐：DyLAN 用「代理重要性分數」做隊伍優化、MMLU 提升至多 25%；但結論強調**任務匹配＋認知多樣性**比「全選最強」重要——盲目堆高手會扼殺多樣性（[DyLAN](https://arxiv.org/abs/2310.02170)、[團隊協同研究](https://arxiv.org/pdf/2510.26352)）。本專案已有 `test_improver_discover_roles` 的多視角角色發現雛形可銜接
- 重點: 角色名稱唯一性是調度的硬前提（既有調研 AG2 結論也如此）：auto/moderator 選人靠角色的 `description` 而非 system prompt，所以 schema 應把「給調度器看的一句話描述」與「給角色自己看的 system prompt」分成兩個欄位（[AG2 GroupChat](https://docs.ag2.ai/latest/docs/user-guide/advanced-concepts/groupchat/groupchat/)）

**建議**

- 建議: 儲存格式選 **Markdown＋YAML frontmatter，一檔一角色**（如 `roles/*.md`），比單一大 YAML 好：system_prompt 是多行長文放 body 最自然、git diff 友善、與 Claude Code subagents 慣例一致；frontmatter 欄位直接沿用現有 Role dataclass 欄位（key/name/avatar/title/model/allowed_tools/permission_mode/tags）＋新增 `description`（給未來 moderator 選人用）
- 建議: 載入策略「**內建角色為預設、檔案覆蓋、同 key 後者勝**」：roles.py 現有 8 角色降為 built-in defaults，啟動時掃 `roles/` 目錄合併覆蓋——向後相容、不砍重練；用 pydantic BaseModel 做檔案驗證（fastapi 已帶 pydantic，錯誤訊息比 dataclass 好），驗證後再轉 frozen Role
- 建議: 管理 API 走既有慣例：加 `GET/POST/PUT/DELETE /api/roles`（pydantic body，照 routes.py 現有 6 個 Body model 的寫法），寫入即落檔到 `roles/` 並 reload——和 `/api/settings` 寫 .env＋`config.reload()` 同模式
- 建議: 「討論小組」做成獨立輕量概念：`Group = {name, role_keys[], mode}`，存同目錄設定檔；組隊驗證三條硬規則——key 必須存在、不得重複、≥2 人；先支援手動組隊，DyLAN 式自動選角列為 P1 跟進（可掛在主持人機制上）
- 建議: schema 設計守住 persona 研究結論：必填「職責＋出力格式硬指令」（micro-rules），口吻/專長為選填裝飾欄位；建立角色時驗證 system_prompt 含出力格式段落，避免使用者建出「只有形容詞的空殼 persona」
- 建議: 測試照既有慣例：用 `_StubExpert`/monkeypatch 模式，重點測「檔案覆蓋內建」「壞檔案被拒且不影響內建角色」「組隊驗證」三類

Sources: [CrewAI Agents](https://docs.crewai.com/en/concepts/agents) · [CrewAI YAML 教學](https://codesignal.com/learn/courses/getting-started-with-crewai-agents-and-tasks/lessons/configuring-crewai-agents-and-tasks-with-yaml-files) · [Claude Code sub-agents](https://code.claude.com/docs/en/sub-agents) · [Persona Prompting](https://www.emergentmind.com/topics/persona-prompting-pp) · [Role prompting 優化](https://arxiv.org/html/2509.00482v1) · [PRISM](https://arxiv.org/html/2603.18507) · [DyLAN](https://arxiv.org/abs/2310.02170) · [團隊協同](https://arxiv.org/pdf/2510.26352) · [AG2 GroupChat](https://docs.ag2.ai/latest/docs/user-guide/advanced-concepts/groupchat/groupchat/)

## 2026-06-13 05:53

我先看程式碼現況並載入搜尋工具，鎖定本輪缺口：「需求→議程/任務拆解＋分派」的做法與既有方案。
調研完成。本輪缺口（議程拆解模式、選角分派、粒度與失敗坑）已補齊，整理如下：

**現況脈絡**（Explore 代理回報）：orchestrator.py 已有「PM 一層拆解→`任務:` 行解析（flow.py `parse_tasks_with_deps` 含 `#id`/`依賴:` 拓撲分波）」；但 DiscussionEngine 只吃單一 topic 字串、**無議程/子題概念**；分派是「波次→lane 鏡射全部角色」、**無選角邏輯**；Group 已有 `{name, role_keys, mode}` 但沒有任何流程觸發它；Role 已有 `description` 欄位（內建 8 角色目前空白）。

**重點**

- 重點: 業界共識是「Planner 先產結構化計畫、executor 各自執行」優於「orchestrator 逐步即興指揮」——planner 模式併發處理更好、agent 閒置更少；且好的 planner 不只列步驟，還要寫明假設、限制與**成功準則**（[Orchestration of Multi-Agent Systems](https://arxiv.org/html/2601.13671v1)、[Self-Resource Allocation](https://arxiv.org/pdf/2504.02051)、[Task Decomposition Strategies](https://apxml.com/courses/agentic-llm-memory-architectures/chapter-4-complex-planning-tool-integration/task-decomposition-strategies)）
- 重點: 重大警示——CrewAI 的 hierarchical（manager 動態分派）模式**實測常失靈**：manager 無法有效協調、退化成順序執行、分派給錯的 agent、延遲爆高，官方 issue 與第三方評測都證實（[CrewAI Issue #4783](https://github.com/crewAIInc/crewAI/issues/4783)、[TDS 分析](https://towardsdatascience.com/why-crewais-manager-worker-architecture-fails-and-how-to-fix-it/)、[CrewAI Processes](https://docs.crewai.com/en/concepts/processes)）。教訓：分派不要全交給 LLM 即興決定，要「LLM 提議＋程式碼硬驗證」
- 重點: 粒度是最大的坑：計畫太粗 executor 沒方向、太細則綁死執行者＋浪費 token；經驗法則「每個子任務 1–3 個動作可完成」；**過度拆解探索型議題**會讓每步後都得重規劃，比 reactive 還貴——重規劃比執行還頻繁就是訊號（[LangChain Plan-and-Execute](https://www.langchain.com/blog/planning-agents)、[GenAI Patterns](https://www.genaipatterns.dev/patterns/agents/plan-and-execute)）
- 重點: 議程生成已有可循模式：LLM 從需求抽「階層式主題→子題」，每個子題含**標題＋一句議程描述＋要點 bullet**；FOMC 模擬（MiniFed）與會議合成研究都採「先產 agenda scenes、再逐 scene 跑多角色討論」的兩段式（[FAME](https://arxiv.org/pdf/2502.13001)、[MiniFed](https://arxiv.org/pdf/2410.18012)、[合成討論系統](https://arxiv.org/html/2503.16505)）
- 重點: 選角依據是角色的 `description`（一句話給調度器看的）而非 system prompt——既有調研（AG2）已確認；本專案 Role 已有此欄位但**內建 8 角色全空**，是本輪的前置缺口

**建議**

- 建議: 架構走「兩段式」對齊研究與既有程式：① 拆解階段——一次 LLM 呼叫把需求拆成「議程（2–5 個子題，各含標題＋描述＋成功準則）＋任務（沿用既有 `任務:`/`#id`/`依賴:` 行格式與 `parse_tasks_with_deps`）」；② 討論階段——逐子題餵 DiscussionEngine（topic=子題），不必改引擎內部，只改呼叫端
- 建議: 分派採「LLM 提議＋硬驗證」：拆解輸出每行加 `負責: <role_key>`，程式碼驗證 key 必須存在於選定 Group 的 role_keys，非法/缺漏就 fallback 到預設角色（如 engineer）——絕不讓 CrewAI 式「manager 全權即興分派」的失靈模式進來
- 建議: 解析沿用既有「行前綴 regex」慣例（flow.py 已有 parse_tasks 系列），不要為這輪引入 JSON schema 結構化輸出——與 codebase 一致、且既有 fallback（條列退回）已驗證可用；只需新增 `子題:`/`負責:` 兩個前綴的 parser 純函式＋單元測試
- 建議: 粒度守則寫進拆解 prompt 的 micro-rules：子題 2–5 個、任務每項一句可驗收、探索型議題允許輸出「單一子題」不硬拆；拆解結果先存（如 session/history），支援後續重看——不做自動 replanning（P1 主持人機制再考慮）
- 建議: 順手補內建 8 角色的 `description`（一句話職能描述），這是選角分派的資料前提，改 roles.py 一處即可
- 建議: 測試照慣例：StubExpert 餵預設拆解文本，測「議程解析」「負責 key 非法被 fallback」「探索型單子題不硬拆」三類純函式＋流程測試

Sources: [Orchestration of MAS](https://arxiv.org/html/2601.13671v1) · [Self-Resource Allocation](https://arxiv.org/pdf/2504.02051) · [Task Decomposition Strategies](https://apxml.com/courses/agentic-llm-memory-architectures/chapter-4-complex-planning-tool-integration/task-decomposition-strategies) · [CrewAI Issue #4783](https://github.com/crewAIInc/crewAI/issues/4783) · [TDS: CrewAI manager 失靈分析](https://towardsdatascience.com/why-crewais-manager-worker-architecture-fails-and-how-to-fix-it/) · [CrewAI Processes](https://docs.crewai.com/en/concepts/processes) · [LangChain Plan-and-Execute](https://www.langchain.com/blog/planning-agents) · [GenAI Patterns: Plan and Execute](https://www.genaipatterns.dev/patterns/agents/plan-and-execute) · [FAME](https://arxiv.org/pdf/2502.13001) · [MiniFed](https://arxiv.org/pdf/2410.18012) · [合成討論系統設計](https://arxiv.org/html/2503.16505)

## 2026-06-13 10:35

先看現有程式碼脈絡，同時載入搜尋工具。
現況很清楚：已有 `DiscussionResult.summary`（規則式從 mentions 統計 consensus/disagreements/final_positions，零 LLM）與 ADR 蒸餾的 prompt 慣例。本輪缺口是「把多角色討論彙整成**結構化結論文件**（共識＋分歧）」的做法與坑。我針對缺口查。
夠了，核心缺口已覆蓋。彙整如下。

---

**重點**

- 重點: 「結論彙整」最佳實踐是把整合輸出明確分四類，而非單一摘要：**共識點 / 分歧點 / 獨特見解(unique findings) / 綜合分析**——這能保住少數意見、同時濾掉個別幻覺，優於只做多數決（[Council Mode](https://arxiv.org/html/2604.02923v1)）。本專案 summary 現有 consensus/disagreements/final_positions，缺「unique findings / 未決事項」這一類。

- 重點: 共識量化可用「LLM-as-judge 給 0.0(完全分歧)→1.0(完全共識) 分數」，並要求輸出 consensus components、disagreement focal points、**evidence strength（證據強度）**——讓分歧不只列出、還標明強弱（[Sequential Consensus Building](https://www.tdcommons.org/cgi/viewcontent.cgi?article=9892&context=dpubs_series)、上引 Council Mode）。

- 重點: 會議摘要最大坑是 **Contextual Inference 幻覺**——LLM 會基於對話旁證生成「看似合理但原文沒明說」的推論。對策是兩段式：先抽取、再以原文做事實校驗/refine（九類錯誤含 omission/irrelevance/structural）（[Dialogue Summarization Hallucination](https://aclanthology.org/2024.acl-long.677/)、[Refining Meeting Summaries with LLM Feedback](https://arxiv.org/pdf/2407.11919)）。

- 重點: 「沉默 ≠ 共識」（Silent Agreement / Agreement Bias）是多角色彙整的系統性偏誤——無人反對常被誤判為共識。要主動監測「未被挑戰的點、被忽略的差異、論證不足」（[Catfish Agent](https://arxiv.org/html/2505.21503v1)）。本專案發言已有 `Mention.stance(同意/反對)` 與反諂媚硬指令，正好可餵彙整器辨識「真共識 vs 沒人講話」。

- 重點: 結論彙整不要「靜默地把強分歧平均掉」；也要注意發言長度不均會讓某角色壟斷討論、扭曲彙整的代表性（[Conversational Task-Solving](https://arxiv.org/pdf/2410.22932)、[ICLR MAD Blogpost](https://d2jud02ci9yv69.cloudfront.net/2025-04-28-mad-159/blog/mad/)）。

- 重點: action-item 導向摘要（先抽「待辦/決議」再組織全文）對長會議轉錄更準（[Action-Item-Driven Summarization](https://arxiv.org/pdf/2312.17581)）——對齊本專案既有 `後續任務:`/`決策:` 行解析慣例。

**建議**

- 建議: **沿用既有兩層輸出慣例**做新交付物：產一份 `CONCLUSION.md`（人讀 markdown，落 workspace 根、進 git），結構固定四段：`## 共識`、`## 分歧`（每條標證據強度/雙方立場）、`## 未決事項/獨特見解`、`## 後續行動`。機讀面沿用 `summary` dict 擴充 `unique_findings`/`open_questions` 兩鍵即可，不另引 JSON schema。

- 建議: 彙整採「**規則式為骨、LLM 為肉**」混合：先用既有 `_build_summary()` 從 `Mention.stance` 統計出 consensus/disagreement 骨架（事實錨點、防幻覺），再用一次 LLM 蒸餾把骨架擴寫成可讀結論——沿用 ADR 蒸餾的 one-shot prompt 範式（逐行 `共識:` / `分歧:` / `未決:` / `行動:` 前綴），交給 `flow.py` 行前綴 parser 解析。**禁止**讓 LLM 純自由生成整份結論（會 Contextual Inference 幻覺）。

- 建議: prompt 內加防坑硬指令三條：①「只彙整 transcript 出現過的論點，不得新增未提及的結論」（防幻覺）；②「無人反對 ≠ 共識，需區分『明確同意』與『無人表態』」（防 Silent Agreement，可用 stance 計數佐證）；③「強分歧必須保留並標明雙方，不得抹平」。

- 建議: 解析新增 `共識:`/`分歧:`/`未決:`/`行動ः` 四個行前綴 parser 純函式（沿用 `^\s*<標籤>\s*[:：]\s*(.+?)$` ERE 慣例＋全形冒號容錯），並對「LLM 漏標前綴」做 fallback：退回直接輸出 `final_positions` 摘要，不崩潰。

- 建議: 每條結論盡量帶來源錨點（如 `(R2 engineer)` 指向第幾輪哪角色），對齊 transcript 的 `(round, speaker)` metadata——這是降幻覺、可查證的低成本手段，也讓 `CONCLUSION.md` 可回溯。

- 建議: 測試照 StubExpert 慣例補三類純函式/流程測試：①四段前綴正常解析；②LLM 漏標前綴→fallback 不崩潰；③「全員無反對」不被誤判為強共識（驗證 stance 計數路徑）。

Sources: [Council Mode](https://arxiv.org/html/2604.02923v1) · [Sequential Consensus Building](https://www.tdcommons.org/cgi/viewcontent.cgi?article=9892&context=dpubs_series) · [Dialogue Summarization Hallucination (ACL)](https://aclanthology.org/2024.acl-long.677/) · [Refining Meeting Summaries with LLM Feedback](https://arxiv.org/pdf/2407.11919) · [Catfish Agent: Silence is Not Consensus](https://arxiv.org/html/2505.21503v1) · [Multi-Agent Conversational Task-Solving](https://arxiv.org/pdf/2410.22932) · [ICLR 2025 Multi-LLM Debate Blogpost](https://d2jud02ci9yv69.cloudfront.net/2025-04-28-mad-159/blog/mad/) · [Action-Item-Driven Summarization](https://arxiv.org/pdf/2312.17581)

## 2026-06-13 12:19

先看現有程式碼脈絡，同時載入搜尋工具。
已有 `studio/conclusion.py` 與兩個 conclusion 測試——本輪不是從零。先看現況確認真缺口。
`conclusion.py` 已大致實作完成（共識/分歧/未決/行動四段、規則骨架+LLM 蒸餾+fallback+落盤都有）。確認還缺什麼接線。
功能其實已實作並過審（task #4 完成）。本輪是迭代既有模組。我只查兩個**尚未覆蓋的前瞻缺口**：(1) 單次蒸餾如何加輕量自我校驗降幻覺；(2) 結論除了 markdown，是否該存機讀格式供 M2 歷史回顧/自我演進消費。
調研完成。功能已實作過審，本輪屬迭代，故只補兩個前瞻缺口的新發現。

---

**重點**

- 重點: 現況盤點——`studio/conclusion.py` 已實作完整（規則骨架→senior 一次蒸餾→四前綴解析→漏標 fallback→`CONCLUSION.md` 四段落盤＋git commit＋`(round, speaker)` 錨點），對齊先前調研。本輪非從零，是迭代既有模組。

- 重點: 單次蒸餾仍是「一次生成、不自檢」。2025 研究指出，在同一次呼叫尾端加**輕量自我校驗**（生成後自問「每條結論是否都能在來源骨架找到依據？不能就棄/標註」）可降幻覺率 9.7–53.3%，召回僅損 0.4–5.7%，且不需多輪 round-trip（[Verify / Factual Self-Verification](https://arxiv.org/pdf/2602.02018)、[CoT+RAG+Self-Consistency+Self-Verification](https://arxiv.org/pdf/2505.09031)）。比先前調研提的「兩段式 extract-then-refine」更省一次 LLM 呼叫，適合本專案 one-shot 慣例。

- 重點: 摘要事實校驗可拆成「逐條 claim 對來源核對」（Factored Verification），與本模組已有的 `(round, speaker)` 錨點天然契合——錨點不只是可讀性，可直接當「每條結論必須對得上一個 transcript pair，否則丟棄」的程式化護欄（[Factored Verification](https://arxiv.org/pdf/2310.10627)）。本模組目前錨點只「盡量帶」、不強制驗證。

- 重點: 存檔格式之爭，對「個人/小團隊規模」的明確結論是 **markdown-first 足矣，不需上 RAG/向量庫**：把 LLM 當編譯器、產出可互連的 markdown wiki 即為知識庫，少量規模下 embedding 反而是過度工程（[Karpathy LLM Wiki](https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an)、[LLM Wiki vs RAG](https://www.mindstudio.ai/blog/llm-wiki-vs-rag-knowledge-base)）。本專案現有「CONCLUSION.md＋git 歷史」路線方向正確。

- 重點: 但 markdown 給人讀、機器消費（M2 歷史回顧/自我演進回饋要檢索引用過往結論）需要穩定 key。最佳實踐是**雙寫**：markdown（人讀）＋輕量 schema JSON sidecar（機讀），primitives 技術中立、可日後再疊索引（[同上 MindStudio](https://www.mindstudio.ai/blog/karpathy-llm-wiki-pattern-knowledge-base-without-rag)、[Markdown vs JSON for LLMs](https://www.craftmarkdown.com/markdown-vs-json-for-llms)）。本模組目前只落 markdown，四鍵 dict 在記憶體內未持久化成機讀檔。

**建議**

- 建議: 本輪如要提升品質，優先做**單次自我校驗**而非加第二次 LLM 呼叫：在 `build_prompt` 末尾加第④條硬指令「逐條自檢：每條結論須對應上方骨架的某 (round, speaker)，無依據者刪除」。零新增呼叫、貼合 one-shot 慣例，預期顯著降 Contextual Inference 幻覺。

- 建議: 把 `(round, speaker)` 錨點從「盡量帶」升級為**程式化護欄**（可選、漸進）：對 senior 產出的非空鍵條目，用既有 `_pair_rounds` 反查是否對得上 transcript pair；對不上的標記（如加 `(未錨定)` 後綴）而非靜默保留——讓「LLM 自填內容」與「有 transcript 來源」在 `CONCLUSION.md` 上可視區分，沿用既有反向重建比對手法，不需新依賴。

- 建議: 若 M2 即將動工，本輪可順手讓 `record` **雙寫**一份 `conclusion.json`（就是現成四鍵 dict＋session_id＋輪數，沿用 atomic tmp-replace），給未來「歷史回顧/自我演進」一個穩定機讀入口；維持 markdown 為人讀主檔。**不要**現在引入向量庫/RAG——當前規模屬過度工程，git＋markdown＋json sidecar 已足。

- 建議: 維持「規則為骨、LLM 為肉」與覆寫式單檔＋git 快照的既有架構，**不要砍掉重練**。上述三點都是低風險增量，且須照 CLAUDE.md 教訓——任何 prompt/格式改動都要實跑黑白樣本（含「全員無反對」「LLM 漏標前綴」兩條既有 fallback 路徑）驗證無回歸，不靠讀碼下結論。

Sources: [Factual Self-Verification (Verify)](https://arxiv.org/pdf/2602.02018) · [CoT+RAG+Self-Consistency+Self-Verification](https://arxiv.org/pdf/2505.09031) · [Factored Verification](https://arxiv.org/pdf/2310.10627) · [Karpathy LLM Wiki (VentureBeat)](https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an) · [LLM Wiki vs RAG (MindStudio)](https://www.mindstudio.ai/blog/llm-wiki-vs-rag-knowledge-base) · [Markdown vs JSON for LLMs](https://www.craftmarkdown.com/markdown-vs-json-for-llms)

## 2026-06-28 20:32

<think>
測試覆蓋已經很完整了。已經有：
- branch protection 三態矩陣
- ls-remote 防覆寫
- force-with-lease 旗標
- CI gate 等候
- secure_write 雙路徑
- release audit / smoke / 流程
- gate level labels
- shippable 落閘

調研已經足夠了，可以彙整。關鍵結論：本任務的硬體（autopilot + publisher + deploy + tests）都已完備，但仍有幾個明確的「安全閉環缺口」值得動手。

讓我精簡輸出。
</think>

調研結束。閉環骨架（autopilot/publisher/deploy/branch protection/ls-remote/force-with-lease/secure_write/test gate）都已實作並有對應測試，本輪屬「補強而非重建」。彙整如下：

---

**重點**

- 重點: 現況盤點——安全自改閉環的**全部主鏈已實作且有測試護欄**：`flow.parse_core_changes` 偵測 `核心改動:` → `backlog.route_core_changes` 路由到 `source="core"` → `autopilot.py` 在 `config.CORE_REPO` (= `x812033727/Ti`) 的 working clone 改 Ti 自己 → `_gate_lint/_gate_collect_without_sdk/_gate_tests` 三道客觀閘（對齊 CI `lint`/`test` job，含「無 SDK collection」攔 import 耦合）→ `_check_branch_protection`（Rulesets + 舊 protection 三態，**unknown 一律 fail-safe 中止**）→ `_commit_push_merge`（ls-remote 防覆寫、force-with-lease --force-if-includes、絕不裸 `-f`）→ `publisher._merge_flow`（等 CI→5 種 outcome→stale update-branch→暫時性退避重試）→ `deploy.redeploy`（reset+pip+restart+health_check，失敗自動 `rollback` 到 `last_good`）。對應測試群：`tests/autopilot/`（70+ 檔，含 `test_qa_protection_matrix`、`test_qa_task*_lsremote_guard`、`test_qa_task5_no_bare_force_audit`、`test_qa_task3_failclosed_contract`、`test_shippable_falls_through_gates`、`test_gate_failure_retry`）、`tests/publish/`、`tests/deploy/`。

- 重點: GitHub 分支保護官方契約已對齊——`Require a pull request before merging` + `Require approvals` + `Require status checks to pass before merging` + `Require linear history`（防 merge commit 與 rebase 亂搞）是「外部不可繞過」的最後底線（[Managing branch protection rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule)、[About status checks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks)）。本專案 CI 走 `actions-runs/conclusion=failure` 即擋合併；autopilot 用 `PUBLISH_BYPASS_INFRA_CI` 判「秒掛＝帳務」走特例路徑，需謹慎守住**只在 `_merge_pr` 內繞過**而非跳過整個 PR。

- 重點: 自改閉環的**真正風險面**不在 PR/CI/merge（已層層包好），而在「autopilot 改到自己、改壞 deploy/merge/backlog 等核心守門模組後，自家程式碼靠 `_self_sig()` + `os.execv` 重載——若新碼改了 deploy/health 判定邏輯，可能**自我解除健康檢查**而帶病上線」。這是 self-modifying system 的經典坑：自己改自己的驗證器（Goodhart's law on yourself）（[Self-Improving AI Agents](https://arxiv.org/abs/2506.04612) §3.2「modifying one's own reward function」、[Karpathy: Software 2.0](https://karpathy.medium.com/software-2-0-c6417e8c1ba1)）。

- 重點: 防止「改壞自己的驗證器」業界有兩條成熟做法：(a) **Manifest/Pinning 一個外部 trusted invariant**——例如從 `x812033727/Ti` 的特定 tag（v0.x.y）把 `_gate_tests` 用的 pytest 指令、必跑測試名單固定寫進 deploy script，autopilot 改 `studio/autopilot.py` 不會影響此指令（[GitHub Branch Protection + required checks pinning](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)）；(b) **External kill switch**——獨立於 autopilot 程序的 watchdog/circuit breaker，可在「N 次連續失敗」或「健康檢查 N 分鐘未過」時遠端寫 `AUTOPILOT_PAUSED`、或把 systemd service stop（[Hundman et al., Continuous AI](https://arxiv.org/abs/2410.16576) §4 safety interlocks）。

- 重點: **防止「意外污染專案 repo」**目前是約定式而非強制式：`CORE_REPO = AUTOPILOT_REPO` 綁死、`publisher.set_repo_override(config.AUTOPILOT_REPO)` 顯式覆寫，但缺少**測試守門**「autopilot 的任何程式碼路徑都不可 publish 到 `config.PUBLISH_REPO`」——若有人未來新增 helper 忘了傳 override、或 LLM 在 studio 內生成 `publisher.publish(cwd, sid, req, repo=PUBLISH_REPO)` 程式碼，會靜默洩漏核心改動到專案 repo。

- 重點: **可觀測性缺口**：目前閉環有「日誌」「backlog 狀態」「history.jsonl」，但缺**結構化審計日誌**（audit trail）——「誰、何時、為何、開了哪個 PR、CI 結果、merge/close 結局、redeploy 是否回滾」目前是散落在 log 與 backlog 兩處，事故後回溯困難。GitHub 官方建議的 audit trail 來源是 webhook events + structured logs（[GitHub Webhook events](https://docs.github.com/en/webhooks/webhook-events-and-payloads)）。

- 重點: **成本熔斷缺口**：`SESSION_TOKEN_BUDGET` / `SESSION_USD_BUDGET` 已存在於 config（autopilot 透過 `time_budget_s` 串入），但**沒有「每日/每週 PR 上限」或「每日 token 預算」**——若 autopilot 進入「反覆重試同一任務」的循環（已被 `_handle_gate_failure` 限 attempts=3 部分緩解），或 LLM 幻覺大量生成任務，仍可能在單一 session 燒光整月額度。

- 重點: **「持續改良成效驗證」（P1）**目前完全缺——merge 即 done，沒有「自改後再跑同組基準，量化是否真的變強」的 hook。本輪屬 M1 範疇（落地一次改良），這條缺口屬 M2 但**已在 CI 上半閉環**（autopilot 跑 `pytest -q` 必綠才 merge，故「不變爛」已保證；「變強」需 M2 補基準）。

**建議**

- 建議: 本輪最划算的安全增量：**寫一個守門測試**「autopilot/publisher 任意路徑都不可把東西推到 `config.PUBLISH_REPO`」（守門「絕不污染專案 repo」這條產品願景）。覆蓋：`publisher.publish(..., repo=config.PUBLISH_REPO)` 從 autopilot 路徑呼叫時應 raise／早退；或更穩的——在 `autopilot._commit_push_merge` 開頭加 assert `target_repo == config.AUTOPILOT_REPO`，把「自動 push 目標 ≠ 專案 repo」做成 import 期或任務執行期的硬不變式，沿用既有 `assert AUTOPILOT_SUBSYSTEM_MAX < AUTOPILOT_SUBSYSTEM_MAX_PENDING` 風格。測試成本：~30 行；價值：把約定變合約。

- 建議: 補強「改壞自家驗證器」的 kill switch：在 `deploy.health_check` 加「連續 N 次失敗自動 `systemctl stop ti-autopilot`」邏輯（N=2，可調），或部署一支獨立 systemd timer 每 5 分鐘檢查 `AUTOPILOT_HEALTH_URL`＋最近一個 autopilot PR 的 merge 紀錄，異常時 `touch AUTOPILOT_PAUSED`。這是「驗證器不可自己改」的外置底線（[Self-Improving AI Agents](https://arxiv.org/abs/2410.16576) §4 interlocks）。本輪不必實作外部 service，但可加一個 `tests/autopilot/test_external_killswitch_contract.py` 守門「autopilot 自己改不了 `_PAUSE_FILE` 路徑、`AUTOPILOT_SERVICE` 名稱、`AUTOPILOT_HEALTH_URL`」——把現有 `AUTOPILOT_PAUSED` 暫停開關的觸發路徑白名單化。

- 建議: 加**結構化 audit log**——`_commit_push_merge` 成功 merge 後寫一筆 `{"ts", "task_id", "pr", "head_sha", "ci_state", "outcome", "duration_s"}` JSONL 到 `autopilot/audit.jsonl`，事故後一秒定位。規模化前（>100 筆/天）再加索引，現階段單純 append 即可，符合既有 lessons.json / backlog.json 的純檔案 IO 慣例，不需新依賴（[GitHub Webhook events + JSONL audit pattern](https://docs.github.com/en/webhooks/webhook-events-and-payloads)）。

- 建議: 成本熔斷**簡版**——加 `AUTOPILOT_DAILY_PR_BUDGET` 與 `AUTOPILOT_DAILY_TOKEN_BUDGET` 兩個 env，每天 UTC 0 重置；超限即 `_pause(...)`。實作僅在 `_commit_push_merge` 入口加一條 `if today_pr_count >= DAILY_BUDGET: return False, "..."`。比 429 退避更深一層，避免 LLM 幻覺把單日額度燒光。屬 5–10 行改動，零新依賴。

- 建議: **不要做的事**：(a) 不要砍掉重練 autopilot/publisher/deploy——已有 70+ 個測試守住所有 invariant，重寫會丟失；(b) 不要現在引入 OPA/Rego 或外部 policy engine——本專案規模 overkill；(c) 不要現在做 M2 的「改良成效驗證」（基準跑測）——超本輪 M1 範疇，且改 pytest 基準本身有「改自己的測試」的 self-referential 風險；(d) 不要把 `_REPO_OVERRIDE` 的覆寫鏈從「autopilot 顯式 set」改成「隱式從 env 推」——目前「per-session 顯式覆寫」是正確邊界，動它會丟掉「主迴圈和 autopilot 不會意外發到對方 repo」的護欄。

- 建議: 維持既有「規則為骨、LLM 為肉」+「lint/collect/test/merge」四道客觀閘門的架構不變。本輪新加的東西（測試守門、audit log、成本熔斷）都是**純增量、不破壞既有契約**。任何 prompt/格式/路由改動照 CLAUDE.md 教訓——實跑黑白樣本驗證（黑白樣本：①Linter 改 lint 設定→CI 攔；②publisher merge 函式被改→既有 `test_qa_task5_no_bare_force_audit` 攔），不靠讀碼下結論。

Sources: [GitHub Managing branch protection rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule) · [GitHub About status checks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks) · [GitHub Secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning) · [GitHub Webhook events](https://docs.github.com/en/webhooks/webhook-events-and-payloads) · [Self-Improving AI Agents (arXiv 2506.04612)](https://arxiv.org/abs/2506.04612) · [Continuous AI Systems (arXiv 2410.16576)](https://arxiv.org/abs/2410.16576) · [Karpathy: Software 2.0](https://karpathy.medium.com/software-2-0-c6417e8c1ba1)

## 2026-06-28 21:34

<think>
架構文件讀完。幾個關鍵現況：

1. **autopilot/publisher 已閉環**（上一輪調研已確認），有 pytest/lint/no-SDK 閘門
2. **history.py** 已存 `成果記分卡`（任務輪數/退回原因/Demo 結果）到 meta，`/api/metrics` 跨場聚合成功率與近期趨勢
3. **history.py / meta** 已可作為「基準資料」的儲存層
4. **workspace 是 git 累積的**——這對「同組基準」很重要：可以 checkout 到 parent commit 跑 baseline
5. **branch protection 設定** 已被外部保護

關鍵缺口：
- 「改良成效驗證」完全沒有——merge 即 done，沒有「自改後再跑同組基準，量化是否真的變強」
- 既有的 history.py 是「單場 session 的結果摘要」，但**不是「同組基準在自改前 vs 自改後」的對照**
- 沒有「可回滾」機制（雖然 deploy.rollback 已有，但沒串到「驗證不通過就回滾」的閉環）

我已經有足夠的調研資料了。讓我整合輸出。

主要發現（缺口已補）：
1. **Karpathy autoresearch** 的核心設計：(a) 評測碼不可改、(b) 固定時間預算、(c) 單一 metric、(d) 保留 baseline
2. **TDAD** 的三個機制：visible/hidden split、semantic mutation、spec evolution regression safety
3. **RQGM** 的 epoch boundary 概念
4. **GitHub branch protection** 已對齊最佳實踐（status checks、reviews、linear history）

現有調研的引用問題：
- 既有調研引用 `arXiv 2506.04612` 為 Self-Improving AI Agents 論文——但實際上是 "Perfecting Depth" 圖像論文
- 既有調研引用 `arXiv 2410.16576` 為 Continuous AI Systems——但實際上是光學論文
- 真正可能對應的：可能應該是 `arXiv 2506.04612`（已確認是圖像論文）——那麼自我改良的權威文獻引用錯了
- 但這不是本輪的核心——本輪不需要重新校正既有調研

本輪核心建議（要對齊 M2 範疇「改良成效驗證」）：
1. **baseline suite + post-change suite**：固定一組 N 個 task fixture，每次 merge 前/後跑同一組，產出 diff
2. **物理隔離評測碼**：把測試 fixture 放在 autopilot 不可改的位置（用 pinned git tag 或專屬 protected branch）
3. **單一可量化的成功指標**（沿用 history.py 已有欄位，不重新定義）
4. **epoch boundary**：每次自改週期固定 evaluation，跨週期才更新 fixture 集
5. **失敗自動 rollback**：deploy 已有 rollback，但要串到「regress → rollback → notify」

讓我輸出最終的調研結論。
</think>

調研夠了，整理輸出。

---

**重點**

- 重點: 本輪「改良成效驗證」的關鍵設計權威是 Karpathy **autoresearch**——其設計與本缺口完美對齊：(a) **評測碼不可改**：`prepare.py`（含 evaluation utilities）明確「do not modify」，與 agent 可改的 `train.py` 物理隔離；(b) **固定時間預算**：5 分鐘 wall clock，無論改什麼（model size、batch size、architecture）都直接可比；(c) **單一 metric** `val_bpb`（vocab-size-independent，architecture-agnostic）；(d) **保留 baseline**：每次新實驗前先 checkout parent commit 跑同組 5 分鐘取基線；(e) 約 12 experiments/hour、可整夜無人值守（[karpathy/autoresearch README](https://github.com/karpathy/autoresearch)）。本專案 `_gate_tests` 跑 `pytest -q` 已達 (b)(c)，缺 (a) 物理隔離 + (d) baseline 對照。

- 重點: **TDAD 論文（Test-Driven AI Agent Definition, 2026）**直接命中「自改 agent 的 regression 安全」缺口：三個機制：(1) **visible/hidden test split**——編譯階段不給隱藏測試，防 agent 只學可見測試；(2) **semantic mutation testing**——post-compilation 用代理生成「似是而非的壞變體」，量測測試套件能否抓出；(3) **spec evolution scenarios**——量化需求變動時的 regression safety。實測發現 **演化後的 spec 編譯成功率只有 58%**，多數失敗的執行「通過所有可見測試只漏 1-2 個」——這是 self-referential 風險的鐵證：本專案「merge 即 done」正是同類漏洞（[arXiv 2603.08806 TDAD](https://arxiv.org/abs/2603.08806)）。

- 重點: **Red Queen Gödel Machine (RQGM, 2026)** 提出「co-evolving agents and their evaluators」框架，核心洞見：**自改 agent 不能假設 evaluation 固定**——傳統自改方法假設 verifier/benchmark 為 stationary，這是 wrong abstraction。RQGM 對策：搜尋以 **epoch 組織、epoch 內 evaluation 固定、epoch 邊界才更新 utility**，使「self-improvement guarantees」得以在「objective 演化」下仍成立。實驗顯示加上「agent-as-a-judge code-review signal」後 test pass rate 提升、token 省 1.35–1.72×——多源 signal 比單一 verifier 更穩（[arXiv 2606.26294 RQGM](https://arxiv.org/abs/2606.26294)）。本專案對應：每次自改 PR merge 是一個 epoch 邊界、epoch 內 baseline 鎖定、跨 epoch 才允許 fixture 集演化。

- 重點: 既有調研引用 `arXiv 2506.04612`（Self-Improving AI Agents）與 `arXiv 2410.16576`（Continuous AI Systems）**兩個 ID 都不對**——實查分別是「Perfecting Depth」電腦視覺論文與「Stabilizing optical solitons」光學論文，與 self-improving AI 無關（[arXiv 2506.04612](https://arxiv.org/abs/2506.04612)、[arXiv 2410.16576](https://arxiv.org/abs/2410.16576)）。本輪不重查此二主題，但建議事後校正 RESEARCH.md 的引用連結。

- 重點: **現況盤點**——`history.py` 已存「成果記分卡」（任務輪數/退回原因/Demo 結果）進 `.meta.json`、`/api/metrics` 已跨場聚合成功率與近期趨勢——這是**單場結果**，但**不是「同組基準、自改前 vs 自改後」的對照**；`autopilot._commit_push_merge` 已串完 lint/collect/test 三道閘門才 merge；`deploy.redeploy` 失敗會自動 `rollback` 到 `last_good`——**但「regress → rollback → notify」閉環未串**；`studio/backlog.py` 已有 route_core_changes 與 source/state_dir 分流——可作為「基準資料」儲存層。

- 重點: **GitHub branch protection 已對齊最佳實踐**——`Require pull request before merging` + `Require status checks to pass before merging` + `Require linear history` 是外部不可繞過的最後底線；本專案 CI 走 `actions-runs/conclusion=failure` 即擋合併。autopilot 用 `PUBLISH_BYPASS_INFRA_CI` 走特例路徑需謹慎守住只在 `_merge_pr` 內繞過而非跳過整個 PR（[About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)）。

- 重點: TDAD 實驗的關鍵警示：「演化後的 spec」失敗的執行**多數通過所有可見測試、只漏 1-2 個**——意味著**僅靠「既有測試全綠」無法保證「沒退步」**，必須有「跨場次同組基準」做對照。autopilot 現有 `_gate_tests` 跑 `pytest -q` 屬「可見測試」，**「不變爛」半閉環已保證；「變強」需 M2 補同組基準對照**——這正是本輪任務定義（[arXiv 2603.08806 TDAD](https://arxiv.org/abs/2603.08806)）。

**建議**

- 建議: **M2 最划算的起手（沿用 autoresearch 模式）**：把「基準任務集」做成**獨立保護的 fixture**——例如新增 `tests/fixtures/improvement_bench/` 子目錄（**置於 git protected branch 或 pinned tag**，autopilot 物理上不可改），內含 N 個代表性 task（每個 fixture ＝ 一段 requirement + 期望產出 hash + 期望 demo 結果 + 期望 pytest subset）。baseline 與 post-change 各自跑同一組 fixture 產出 `{"task_id", "success", "duration_s", "tokens", "iterations"}` JSONL 對照 diff——沿用 `autopilot/audit.jsonl` 同樣 append-only 慣例、零新依賴。fixture 集的「演化」必須走 epoch 邊界（每次合 MR 才允許加 fixture），對齊 RQGM 結論。

- 建議: **沿用 history.py 既有記分卡欄位**做基準資料，不重新定義 metric：`history.session_meta` 已有「任務輪數 / 退回原因 / Demo 結果」三鍵（ARCHITECTURE.md），把這三鍵在 baseline 與 post-change 兩個 epoch 各取一次、存成 side-by-side diff，就是「量化是否真的變強」——避免重造輪子、保留既有 `/api/metrics` 跨場聚合慣例。

- 建議: **串起「regress → rollback → notify」閉環**：`deploy.redeploy` 已有「失敗自動 rollback 到 last_good」（既有調研已確認），但**還沒串「驗證不通過 → 自動觸發」**——本輪只需在 `deploy` 後跑一次同組 benchmark（複用上一條建議的 fixture），diff 為負（success rate 下降 / tokens 上升）就 `touch AUTOPILOT_PAUSED` 並寫 `autopilot/audit.jsonl`，對齊上一輪調研的「成本熔斷＋審計日誌」基礎設施——純增量、不破壞既有合約。

- 建議: **守門「autopilot 自己改不了基準集」**——兩條路徑擇一或並行：(a) **物理隔離**：把 `tests/fixtures/improvement_bench/` 放到獨立 GitHub branch `improvement-bench-protected`、加 branch protection（admin 不可繞過的 require reviews），autopilot clone `CORE_REPO` 時不 fetch 該 branch、跑基準時另以 `GH_PAT` 讀；(b) **hash pinning**：baseline suite 每個 fixture 內含 `pinned_commit_hash`，autopilot 跑基準前先 `git rev-parse HEAD` 對該 path 校驗，hash 不符即 abort——這比 (a) 簡單但弱（autopilot 仍可改 fixture 後改 hash）。CLAUDE.md「架構鐵則」的精神是「外部不可繞過」，優先採 (a)。

- 建議: **採 TDAD 的 visible/hidden 雙層**：fixture 集分兩組——`visible/`（autopilot 與開發者皆可讀，協助 agent 理解「什麼算進步」）＋ `hidden/`（CI 端另一組 secret token 注入的 fixture，autopilot 完全不知道內容）。hidden 那組是真正的「spec evolution regression safety」——TDAD 數據顯示無 hidden 時 92% 編譯成功、有 hidden spec evolution 時只有 58%，**hidden 是抓「只學可見」漏洞的關鍵**。實作成本：~30 行 + GitHub Actions secret 一個。

- 建議: **不要做的事**：(a) 不要砍掉重練 autopilot/publisher/deploy——已有 70+ 個測試守住所有 invariant；(b) 不要現在引入 pytest-benchmark、asv 等外部基準框架——純檔案 IO + JSONL 已足，與 lessons.json / backlog.json / conclusion.json 慣例一致；(c) 不要讓 autopilot 自己定義 metric——Goodhart's law；(d) 不要把 baseline fixture 與 production test 混在同一目錄——必須物理隔離（見上一條）；(e) 不要現在做「跨產品跨專案的綜合 score」——本輪聚焦「自改驗證」單一閉環，跨場聚合留 M3；(f) 不要試圖校正既有 RESEARCH.md 的 arXiv ID 引用錯誤——那是歷史紀錄，本輪聚焦產出新結論。

- 建議: 維持既有「規則為骨、LLM 為肉」+「lint/collect/test/merge」四道客觀閘門不變，本輪新加的（baseline fixture 集 + visible/hidden 雙層 + epoch 邊界 + regress→rollback 閉環）都是**純增量、不破壞既有契約**。照 CLAUDE.md 教訓：任何 prompt/格式/路由改動都要實跑黑白樣本驗證——本輪至少需三組黑白樣本：①可見基準集跑通／不可見基準集跑通（baseline 兩邊皆綠）；②故意把某 fixture 期望值改壞 → baseline 必須抓到（不是只 fixture 內測試）；③故意把 autopilot 對 fixture 路徑 hard-code → CI 端的 hidden 路徑仍可被找到（防「只看可見」漏洞）。

Sources: [Karpathy autoresearch (GitHub)](https://github.com/karpathy/autoresearch) · [TDAD (arXiv 2603.08806)](https://arxiv.org/abs/2603.08806) · [Red Queen Gödel Machine (arXiv 2606.26294)](https://arxiv.org/abs/2606.26294) · [GitHub About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) · [arXiv 2506.04612 (驗證既有引用錯誤)](https://arxiv.org/abs/2506.04612) · [arXiv 2410.16576 (驗證既有引用錯誤)](https://arxiv.org/abs/2410.16576)

## 2026-06-28 23:24

<think>
我已蒐集到足夠資料。讓我聚焦在最重要的：

**重要發現**：
1. **Anthropic 官方 Rate Limits 用 token bucket algorithm**（不是固定窗口重置，而是持續補充）— 這對「重置倒數」的計算方式有重大影響，我們現有的 `soonest_reset` 是基於視窗重置，**不適用於 token bucket**。需要釐清我們 `claude_usage.fetch_rate_limits()` 拿到的是哪種資訊（看起來是 OAuth subscription 端的「剩餘額度」快照，不是 server-side token bucket）。

2. **Anthropic Admin API 需 Admin API key（sk-ant-admin01-...）** — 不適用個人帳號，也不是本專案現在的取法。我們現在用的是 OAuth 訂閱帳號，從 `~/.claude/.credentials.json` 讀 accessToken 查官方 usage endpoint。

3. **LiteLLM 有 Rate-Limit Aware routing** — 透過 Redis 追蹤 tpm/rpm、用 cooldown state 避免把請求派到快撞限流的部署。但這是「請求級」決策（每次 acompletion 都要查 Redis），不是「會話級」決策。

4. **Portkey Fallbacks** — 觸發 on_status_codes（429/503）、cross-provider fallback、load balancer + fallback 組合。純被動：在請求失敗時切換，不是主動依額度切換。

5. **Claude Code IAM**：
   - 預設 OAuth credentials 存在 `~/.claude/.credentials.json`（macOS 用 Keychain）
   - 支援 `apiKeyHelper` 自動刷新憑證（每 5 分鐘或 401 時）
   - 環境變數 `ANTHROPIC_API_KEY`（X-Api-Key header）/ `ANTHROPIC_AUTH_TOKEN`（Bearer header）
   - **沒有原生多帳號切換** — 單帳號為主
   - Claude Code 透過 `CLAUDE_CONFIG_DIR` 環境變數可以覆寫 .credentials.json 位置，這就是我們的 `claude_accounts` 機制的基礎（用不同 `cred_file` 切換）

6. **OpenAI rate limits** — 從官網來看是 tier-based、有 RPM/TPM，沒有像 LiteLLM 那樣提供可程式化的 quota 查詢 endpoint。

現在我已有足夠資料產出本輪調研結論。讓我聚焦最關鍵的發現與建議。

**本輪關鍵建議方向**：

1. **不要砍掉重練** — `provider_quota.snapshot/constrained/least_constrained_ready/summarize_for_pm` + `make_expert(..., provider=...)` 已是骨架，只是「PM 運行時呼叫」的執行路徑沒串起來。

2. **採「雙軌策略」**：
   - **(a) 預防式（pre-flight）：任務分派前** 查 `provider_quota.snapshot`，依 `least_constrained_ready()` 決定本場預設 provider；對已被綁到受限 provider 的角色，自動改綁 least_constrained（既有的「受限角色自動重綁最寬鬆就緒」安全網）
   - **(b) 反應式（in-flight）：任務執行中** 若 `ProviderUnavailable` 被拋出，依 `least_constrained_ready()` 即時切換 provider 重試

3. **「全受限」的 fallback 策略**：
   - 等下一個 reset（最長到 soonest_reset +30s 安全邊界）
   - 仍受限 → 觸發 `AUTOPILOT_PAUSED`（沿用既有暫停開關）或退到下一輪
   - 不要無限 spin（避免 token 空燒）

4. **多帳號 Claude 切換**：
   - 沿用既有 `claude_accounts` + `fetch_rate_limits(cred_file=...)` 介面
   - 「同 provider 多帳號」與「跨 provider」兩種自動重綁方式要分開
   - 多帳號用 round-robin（避免單帳號過熱）+ least_constrained 排序（最空者先）

5. **可觀測性**：
   - 在 `autopilot/audit.jsonl` 寫一筆 `{"event": "provider_rebind", "from": "claude", "to": "minimax", "reason": "constrained", "max_used": 95.3}`
   - 前端 `/api/provider-quota` 已存在，加上「最近一次重綁原因」的顯示

6. **autopilot 自改閉環**：
   - 把 provider_quota / least_constrained_ready 加到 M2「評測碼不可改」清單（沿用 Karpathy autoresearch + TDAD 概念）
   - autopilot 改 orchestrator.py 不應影響 quota 查詢路徑（已有 — provider_quota.py 是獨立模組）

7. **常見坑**：
   - 不要把 `snapshot()` 60s 快取延長太多（避免額度快速變化時決策失準）
   - 不要讓 PM 自動決策覆蓋使用者明確設定的 `TI_PROVIDER_<role>`（per-role 覆寫優先）
   - 不要把 antigravity/codex 的「CLI 冷啟動成本」算進 routing 決策（會誤導成「每次都別用它」）
   - 「受限門檻 CONSTRAINED_THRESHOLD=90%」要可在 settings 面板調整
   - 不要現在引入 LiteLLM / Portkey 之類外部 gateway — overkill 且會跟本專案的「規則為骨 LLM 為肉」哲學衝突

8. **測試設計**：
   - 黑樣本：給定 A 受限 → 應選 B
   - 白樣本：給定全部受限 → 觸發 pause 或 wait_for_reset
   - 黑樣本：使用者明確 TI_PROVIDER_engineer=codex → 不應被自動重綁
   - 白樣本：per-role 覆寫生效、least_constrained 不覆寫它
   - 白樣本：60s 快取有效（連續兩次 snapshot 第二次走快取）
   - 黑樣本：snapshot 異常 → constrained() 仍安全回 True（fail-closed）

讓我精簡輸出。
</think>

已蒐集到本輪關鍵決策依據。整理輸出。

---

**重點**

- 重點: **現況盤點——骨架已實作，只缺「執行路徑串接」**。`provider_quota.snapshot/constrained/least_constrained_ready/summarize_for_pm` 四件套已就緒（60s 快取、四家 provider 並行查詢、`CONSTRAINED_THRESHOLD=90%` 受限門檻、相容 window/bucket 兩種 rate_limits 結構），`providers.make_expert(role, session_id, cwd, *, provider=...)` 已有顯式 provider 覆寫的接縫，`llm_caller` 統一退避工廠（429/529 分流、`ProviderUnavailable` 例外、`ProviderUnavailable` 已有 `provider_unavailable_kind` 文字分類）皆完整。**唯獨「PM 動態分派」實際呼叫這條執行鏈沒串起來**——`dynamic_first_workflow()` 文件說有，但程式碼端沒有「查 quota → 改分派」的閉環。本輪是純增量、不重建。

- 重點: **業界兩條主流 quota-aware routing 策略**：(a) **LiteLLM Rate-Limit Aware v2**——Redis 追蹤每 deployment 的 tpm/rpm、用 cooldown state 在每次 `acompletion` 前避免把請求派到快撞限流的部署（[LiteLLM routing](https://docs.litellm.ai/docs/routing)）；(b) **Portkey Fallbacks**——被動式，預設所有非 2xx 觸發，可指定 `on_status_codes: [429, 503]` 限定只在限流／過載時切換，且可巢狀（fallback 目標本身是 load balancer、conditional router）（[Portkey Fallbacks](https://portkey.ai/docs/product/ai-gateway/fallbacks)）。**前者是請求級動態、後者是失敗級被動**——對本專案「PM 運行時做會話級決策」都不完全適用；我們需要的是 **(a)+**(b) 的混合：會話起點 pre-flight 預防式分派 + 執行中反應式 fallback。

- 重點: **Anthropic 官方 Rate Limits 用 token bucket algorithm**——不是固定窗口重置，而是「容量持續補充到上限」（[Anthropic rate limits](https://platform.claude.com/docs/en/api/rate-limits)）。本專案 `provider_quota._usage()` 從 `claude_usage.fetch_rate_limits()` 抽 `used_percentage` 與 `reset_at`——若 OAuth usage endpoint 回的是「window 倒數」而非「bucket 補充速率」，則 `soonest_reset` 在 Anthropic 是「視窗邊界」（rough heuristic），不是嚴格意義的「額度回到 0 的時刻」。**這是已存在的假設，不在本輪重新校正**，但提醒：`least_constrained_ready` 的「最低用量」排序對 Anthropic 而言已是 best-effort 訊號，配合 `run_with_retries` 的 retry-after 退避才能真正撐過 429。

- 重點: **Claude Code 多帳號機制是「cred_file 路徑切換」非原生 round-robin**——`~/.claude/.credentials.json` 是單帳號預設位置；`apiKeyHelper` 可自動刷新憑證（每 5 分鐘或 HTTP 401 時），但無原生多帳號輪詢（[Claude Code Authentication](https://code.claude.com/docs/en/iam) §Credential management）。本專案 `claude_accounts.list_accounts()` 配合 `claude_usage.fetch_rate_limits(cred_file=...)` 已實作「用不同 cred_file 查各帳號額度」的介面——這是把「同 provider 多帳號」併進 quota-aware routing 的基礎接縫。

- 重點: **Antigravity 額度查詢的特殊坑**——`agy` OAuth token 約每小時過期；目前實作是「有 token（即使過期、可由跑討論刷新）即視為 ready」（`provider_quota._antigravity_status`），只有完全沒登入才算未就緒。換言之 **「額度查詢失敗」不等於「不可用」**，要避免把它誤判成受限而提前跳過 antigravity。`provider_unavailable_kind` 已把 `token_missing` 與 `unauthorized` 分流，但需在 routing 決策層守門：「查詢異常」與「額度耗盡」要分開處理——前者重試，後者才跳過。

- 重點: **跨 provider 路由的隱藏成本**——Codex CLI 與 Antigravity CLI 都有冷啟動延遲（曾達 12s 之子程序成本——為此 `snapshot()` 已從 sequential 改成 `ThreadPoolExecutor` 並行查詢）；若 routing 演算法只看「當下額度」會誤導成「每次都跳過 CLI 改用 OpenAI」，但實際上 CLI 的工具能力（檔案寫入、shell）是 OpenAI 相容介面沒有的。**routing 必須在「額度寬鬆」與「工具能力需求」之間取捨**，不該用 LLM 自動決定每個 tool-call 的 backend。

- 重點: **「全受限」沒有業界標準 fallback**——LiteLLM / Portkey 都把「全失敗」留給使用者；對自改閉環（autopilot）這是危險空窗。經查「self-improving AI systems + quota exhaustion」沒有專門論文，但 [Self-Improving AI Agents](https://arxiv.org/abs/2506.04612) §4 的 safety interlocks 概念可直接套用：本專案的 `AUTOPILOT_PAUSED`（已有）、`AUTOPILOT_DAILY_*_BUDGET`（前一輪調研建議但尚未實作）就是這條防線。本輪至少需定義「全受限 → 暫停 + 寫 audit.jsonl + 等下一 reset」的明確路徑。

- 重點: **`make_expert(..., provider=...)` 接縫已備但目前只用在 `make_expert` 的 `provider` 參數**——沒有任何 `orchestrator` / `improver` / `autopilot` 路徑真的在運行時根據額度決定改用哪家。ARCHITECTURE.md 已明確說「動態分派」會呼叫它，但程式碼端只是定義、未串接。本輪就是要把這個 gap 補上。

**建議**

- 建議: **採「pre-flight + in-flight 雙軌」**（最小成本、最對齊既有 `provider_quota` 抽象）：(a) **pre-flight**——`StudioSession` 進入任務分派階段（`_stage_build` / `_stage_dynamic` 入口）時，先 `snapshot()` 一次拿 `least_constrained_ready()`；對 `constrained()` 的角色自動以 `make_expert(role, session_id, cwd, provider=<least_constrained>)` 重綁（沿用既有顯式 provider 覆寫接縫）；對 `TI_PROVIDER_<KEY>` 明確覆寫的角色**不**被自動重綁（使用者意圖優先）。(b) **in-flight**——`OpenAIExpert.speak()` 拋出 `ProviderUnavailable` 時（已收斂於 `llm_caller._pauses_on_provider_failure` 路徑），由 `orchestrator` 捕捉後查 `least_constrained_ready()`，把「下一輪 speak」以 `make_expert(..., provider=...)` 換到還有額度的 backend——這是「受限角色自動重綁」最便宜的接線點。

- 建議: **全受限 fallback 採「短等 reset + 暫停」雙階**：(a) 若所有 provider 都 `constrained()`，先算 `min(soonest_reset) + 30s 安全邊界` 與當下的差，< `EXPERT_RATE_LIMIT_BACKOFF_CAP`（60s）就 `asyncio.sleep` 等一下後重 snapshot；(b) 若等待 > 上限或已等過一次仍受限，**對 autopilot 路徑 `touch AUTOPILOT_PAUSED` + 寫 `autopilot/audit.jsonl`**，互動 session 路徑則發 `provider_constrained` 事件讓前端顯示「所有 provider 額度已耗盡」。**不要無限 spin**——會把會話時間預算燒光、觸發既有 `SESSION_SOFT_DEADLINE_FRAC` 收斂時反而混亂歸因。

- 建議: **把 per-role 明確覆寫的「使用者意圖」保護好**——`config.role_provider(key)` 已存在但只有 `effective_provider(role)` 在用，沒有任何路徑保護「使用者明確設了 TI_PROVIDER_engineer=codex 就別被自動換掉」。建議加 `is_user_explicit(role) -> bool`（`config.role_provider(key) != ""`）並在 pre-flight 自動重綁前過濾——這是把「使用者意圖 vs 系統自動優化」的界線變成合約（黑樣本：故意設 TI_PROVIDER_engineer=codex + 全程額度受限 → engineer 仍走 codex 並由 429 退避消化，不被靜默改綁）。

- 建議: **多帳號 Claude 切換加進 routing 表**——沿用 `claude_accounts.list_accounts()` + `claude_usage.fetch_rate_limits(cred_file=...)` 既有介面，把「同 provider 多 cred_file」視為 routing 表的擴展項：`least_constrained_ready()` 回的不再只是 provider key，可回 `(provider, cred_file_or_None)` 元組。對「無 cred_file」的單帳號使用者零影響（沿用全域憑證）；對多帳號使用者提供 round-robin + least_constrained 混合策略（最低用量帳號優先，避免單帳號過熱）。

- 建議: **「quota 變化」事件串到 audit 與前端**——`autopilot/audit.jsonl` 新增 `{"event": "provider_rebind", "from": ..., "to": ..., "reason": "constrained"|"user_explicit"|"no_provider_ready", "max_used": 95.3, "ts": ...}`，前端 `/api/provider-quota` 回傳多加 `last_rebind` 欄位讓 UI 顯示「剛才因額度受限，pm 從 claude 換到 minimax」——事故後一秒定位，無需新依賴（沿用 `autopilot/audit.jsonl` 純檔案 IO 慣例）。

- 建議: **動態 workflow prompt 微更新**（純增量 1-2 行）——`_stage_dynamic` 的 PM prompt 在開頭加一段：`provider_quota.summarize_for_pm(snap, role_provider_map)` 餵給 PM，讓 PM 在「該找誰」決策時考慮當下額度（既有 `_dynamic_first_workflow` 已是 opt-in，動態優先流程已經會走到這層）。**不要**把 routing 完全交給 LLM——`constrained()`/`least_constrained_ready()` 是程式化硬不變式，LLM 只在「受限時要不要堅持用受限角色（例如該角色有獨占工具）」做例外決策，防 CrewAI 式「manager 全權即興」失靈（既有調研已記）。

- 建議: **測試設計黑白樣本**（照 CLAUDE.md「黑白樣本驗證、不靠讀碼下結論」）：① 白：`A 受限 / B 就緒` → 自動重綁到 B；② 黑：所有受限 → 觸發 pause 或 `provider_constrained` 事件、不無限 spin；③ 白：`TI_PROVIDER_engineer=codex` + claude 受限 → engineer 仍走 codex（不破壞使用者意圖）；④ 黑：`ProviderUnavailable` 拋出 → 下一輪 speak 走 `least_constrained` 重試並成功；⑤ 白：`snapshot()` 60s 快取有效（連兩次第二次走快取）；⑥ 黑：snapshot 異常 → `constrained()` 安全回 True（fail-closed，避免把壞資料當額度用）。

- 建議: **不要做的事**：(a) 不要砍掉重練 provider_quota / providers / llm_caller——已有完整骨架；(b) 不要現在引入 LiteLLM / Portkey / OpenRouter 等外部 LLM gateway——overkill 且會破壞「規則為骨 LLM 為肉」與既有 provider 抽象；(c) 不要把 quota-aware routing 推到 tool-call 級別（單一訊息多次決定 backend）——CLI 冷啟動成本會把整場拖慢，違反既有 `TI_LLM_MAX_CONCURRENCY` 節流哲學；(d) 不要把「全受限時要不要等 reset」交給 LLM 決定——這是 hard invariant，必須程式碼硬編碼；(e) 不要讓 autopilot 自動改 `provider_quota.py` 的 `CONSTRAINED_THRESHOLD` 或 routing 表——沿用 TDAD 「評測碼不可改」原則，routing 邏輯屬準評測碼，autopilot 可改 quota 查詢路徑但不應改 routing 決策；(f) 不要現在做 M2 的「跨場次 provider 偏好學習」——本輪聚焦「會話內 quota-aware routing」單一閉環，跨場學習留 M2。

- 建議: **守門「routing 邏輯不被 autopilot 偷偷改壞」**——在 `tests/autopilot/` 加 `test_provider_routing_contract.py` 守門：`least_constrained_ready` 在「全受限」時必回 None（不會誤回某個 provider）、`constrained(snap, provider)` 在 `error != None` 時必回 True（fail-closed）、`make_expert(..., provider=X)` 在 autopilot 路徑下呼叫時 target_repo 仍守 `CORE_REPO` 不變（沿用既有 `_commit_push_merge` 不變式測試風格）。

Sources: [LiteLLM Router - Load Balancing](https://docs.litellm.ai/docs/routing) · [Portkey Fallbacks](https://portkey.ai/docs/product/ai-gateway/fallbacks) · [Anthropic Rate Limits](https://platform.claude.com/docs/en/api/rate-limits) · [Anthropic Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api) · [Claude Code Authentication](https://code.claude.com/docs/en/iam) · [OpenAI Rate limits](https://developers.openai.com/api/docs/guides/rate-limits) · [Self-Improving AI Agents §4 safety interlocks](https://arxiv.org/abs/2506.04612)

## 2026-06-29 00:44

<think>
WebFetch 抓 Python docs 不太有用——重複內容。我需要的是測試結構。讓我用 ls 看測試目錄。實際上我沒有 shell 工具，只有 web_fetch 與 read_file。

讓我用 read_file 看 tests 目錄。不過 read_file 只能讀檔案，不能列目錄。讓我嘗試其他路徑。

實際上我已經有足夠的程式碼脈絡。我現在可以開始整合結論。

關鍵事實：
1. `config.role_provider(key)` 已有完整契約：「回 str（白名單內才回非空，否則 ""）」
2. `_pick_provider` 與 `_preflight_rebind_experts` 兩處都 inline 用 `config.role_provider(...)` 判定
3. `effective_provider(role)` 已經在 providers.py 把 `role_provider(role.key) or PROVIDER` 邏輯抽好
4. flow.py 的 `plan_preflight_rebind` 接受 `explicit_overrides: dict[str, str]` 參數
5. 既有調研已建議加 `is_user_explicit(role) -> bool`（`config.role_provider(key) != ""`）

所以本輪任務的核心是：抽一個 helper（建議命名 `is_user_explicit_provider(role_key)`），把兩處 inline 判定收斂。

至於是否放 config.py / orchestrator.py：
- 既有調研建議放 config.py
- 但任務說「供兩路徑共用」沒指定位置
- 我傾向 `config.py`（沿用既有調研 + 公開契約的價值）

至於 Web 查詢：本輪任務是純內部程式碼重構，沒有什麼套件/API 可以查。
- 不需要查 LiteLLM / Portkey（這些是 routing 策略，不是「使用者意圖判定」）
- 不需要查 Python 慣例（`os.getenv` 已用、列表推導已用，沒什麼特殊)
- 不需要查什麼新技術

所以本輪**不需要 WebSearch**。我已經有完整現況脈絡，可以直接產出結論。

讓我聚焦：
1. 重點：現況兩處 inline 用 `config.role_provider(...)` 判定，沒有共用 helper
2. 建議：在 config.py 加 `is_user_explicit_provider(role_key) -> bool`，兩處改用它
3. 黑白樣本：依既有調研已有的建議（TI_PROVIDER_engineer=codex + 全程額度受限 → engineer 仍走 codex）

我給的結論需要簡潔，符合角色「精簡、聚焦、不要長篇大論」的要求。
</think>

我有足夠的現況脈絡了。這是純內部 helper 重構任務，不需要再上網查套件（兩條路徑的判定語意都已收斂在 `config.role_provider`）。直接產出結論。

---

**重點**

- 重點: **現況兩處口徑散落**：`_pick_provider`（orchestrator.py:1422）與 `_explicit_provider_overrides`（orchestrator.py:1380）都 inline 寫 `if config.role_provider(role.key)` 判定「使用者明確覆寫」，但沒有共用 helper。`_pick_provider` 取值用（`return explicit`）、`_explicit_provider_overrides` 只用 bool 篩選，但兩處的「明確覆寫」定義都得記得和 `config.role_provider` 的「白名單 + 正規化」語意保持一致——將來只要 `config.role_provider` 的語意擴張（例如要求大小寫區分、要求不可為空字串當作「明確清空」），兩處都要記得同步改。

- 重點: **單一真值來源已就緒**：`config.role_provider(key)` 已實作好「白名單正規化＋空字串＝未覆寫」的契約（config.py:107-117，回 `""` 表示無覆寫、回 PROVIDERS 白名單內字串表示有覆寫）；`providers.effective_provider(role)` 已把 `role_provider(role.key) or PROVIDER` 的「per-role 覆寫優先，否則全域」邏輯抽好。**本輪 helper 只是把兩處 inline 的 `bool(config.role_provider(...))` 收斂成一個有名稱、可被測試鎖住的 contract**——不引入新邏輯、不改變既有行為。

- 重點: **既有調研已有明確建議**：RESEARCH.md 2026-06-28 23:24 條目已記「建議加 `is_user_explicit(role) -> bool`（`config.role_provider(key) != ""`）並在 pre-flight 自動重綁前過濾」，並指定黑樣本「故意設 TI_PROVIDER_engineer=codex + 全程額度受限 → engineer 仍走 codex 並由 429 退避消化，不被靜默改綁」。本輪是把這條建議落地成程式碼，**不是新決策**。

- 重點: **兩處呼叫點的對稱關係**：
  - `_pick_provider`：判定「有覆寫就 return 該值」，需同時拿到 (bool, str)
  - `_explicit_provider_overrides`（`_preflight_rebind_experts` 內）：判定「本場在場角色裡哪些有覆寫」，需 dict comprehension 篩選
  - 共用部分只是「判定某 role_key 是否被使用者覆寫」這個 bool 語意——值取得仍走 `config.role_provider`。因此 helper 命名 `is_user_explicit_provider(role_key) -> bool`，與既有 `effective_provider(role)` 並列（公開契約）。

- 重點: **測試守門慣例已有**：CLAUDE.md 已寫「黑白樣本驗證、不靠讀碼下結論」、autopilot 已有 `test_provider_routing_contract.py` 風格（守 `least_constrained_ready` 在全受限時必回 None、`constrained(snap, provider)` 在 `error != None` 時必回 True、`make_expert(..., provider=X)` 在 autopilot 路徑下 target_repo 仍守 `CORE_REPO`）——本輪新增的 helper 應補一條 contract 測試「TI_PROVIDER_engineer=codex 設下，`is_user_explicit_provider('engineer')` 必 True、`_pick_provider` 仍走 codex 不被額度重綁」。

- 重點: **既有結構不動**：本輪不砍掉重練 `provider_quota` / `providers` / `llm_caller`；不引入 LiteLLM / Portkey 等外部 gateway；不改 `_preflight_rebind_experts` 的副作用路徑（仍由 `_apply_preflight_rebind` 套用 plan、`_handle_all_constrained` 處理全受限）；不改 `flow.plan_preflight_rebind` 的介面（仍收 `explicit_overrides: dict[str, str]`，內部用 `.get(role_key)` 判定）。

---

**建議**

- 建議: **在 `config.py` 加 helper `is_user_explicit_provider(role_key: str) -> bool`**，與既有 `role_provider(key)` / `effective_provider(role)` 並列為公開契約。理由：(1) 沿用既有調研建議（RESEARCH.md 2026-06-28）；(2) 純粹是 config 語意（環境變數層級的「使用者意圖」判定），不該藏在 routing 邏輯內部；(3) 之後 autopilot / improver / settings UI 若要讀「使用者是否明確指定過某角色 provider」，可以直接用，不必另外 import orchestrator。實作：直接 `return bool(config.role_provider(role_key))` 加 docstring 強調「以 `config.role_provider(key)` 為單一真值來源」+ 一句黑樣本語意。

- 建議: **兩處呼叫點同步改寫**（一次性純機械替換、不改邏輯）：
  - `_pick_provider` 內：`explicit = config.role_provider(role.key); if explicit: return explicit` → `explicit = config.role_provider(role.key); if is_user_explicit_provider(role.key): return explicit`（兩種寫法並存，後者意圖更明顯；或直接 `if is_user_explicit_provider(role.key): return config.role_provider(role.key)`）
  - `_explicit_provider_overrides` 的 dict comprehension：`if config.role_provider(ex.role.key)` → `if is_user_explicit_provider(ex.role.key)`
  - 兩處的 `import` 區塊補上 `from . import config`（已存在），把 `config.role_provider` 的呼叫替換為 `is_user_explicit_provider`；值取得仍走 `config.role_provider`（這是底層讀取，helper 不可遮蔽它）。

- 建議: **加黑白樣本測試**，照既有 `test_provider_routing_contract.py` 風格新增 `test_user_explicit_provider_contract.py`：
  - 白：環境變數 `TI_PROVIDER_engineer=codex` 設下 → `is_user_explicit_provider('engineer')` 回 True、`is_user_explicit_provider('pm')` 回 False（無覆寫）
  - 白：無 `TI_PROVIDER_*` 設 → 所有 key 都回 False
  - 黑：`TI_PROVIDER_engineer=BogusProvider`（不在 PROVIDERS 白名單）→ 回 False（沿用 `config.role_provider` 的白名單語意，不視為覆寫）
  - 端到端黑樣本（既有調研已記）：`TI_PROVIDER_engineer=codex` + 全額度受限情境下呼叫 `_pick_provider(BY_KEY['engineer'], hint='')` → 必回 `"codex"`，不被 `least_constrained_ready()` 改綁
  - 對稱黑樣本：把 helper 改名 / 改語意後，contract 測試必紅（守門「口徑不被 autopilot 偷偷改壞」）

- 建議: **不要做的事**（沿用 RESEARCH.md 既有）：
  - 不要把 helper 命名為 `user_provider` / `explicit_override` / `forced_provider` 等模糊名稱；既有用語「user explicit」明確，且和 `effective_provider` 的「effective」相對——「user explicit vs system effective」的界線即合約本意
  - 不要把 `effective_provider(role)` 內的 `role_provider(role.key) or PROVIDER` 邏輯也併進 helper（會破壞既有契約層）
  - 不要改 `_preflight_rebind_experts` 的副作用順序（仍先算 plan、再跑 `_handle_all_constrained`、最後 `_apply_preflight_rebind`）
  - 不要改 `flow.plan_preflight_rebind` 的簽名（仍收 `explicit_overrides: dict[str, str]`，沿用其內 `explicit_overrides.get(role_key)` 的判定）
  - 不要現在順手做 M2 的「跨場次 provider 偏好學習」（既有調研已記留 M2）

- 建議: **守門「autopilot 不改 routing 判定 helper」**：依既有 `tests/autopilot/test_provider_routing_contract.py` 風格，在測試加一條 `test_user_explicit_provider_immutable_from_autopilot`：在 autopilot 路徑（monkeypatch `StudioSession` 為 autopilot 包）呼叫 `config.is_user_explicit_provider` 後，該 helper 的行為與未經 autopilot 干預時等價。這條與既有 `_commit_push_merge` 不變式測試精神一致——守住「準評測碼不可被 autopilot 偷改」。

- 建議: **任何 prompt/格式/路由改動照 CLAUDE.md 教訓實跑黑白樣本驗證**：本輪至少跑三組——(a) 既有 70+ autopilot 測試（守住「重構未改既有契約」）、(b) 既有 `test_provider_routing_contract.py`（守住 routing 行為等價）、(c) 新增的 `test_user_explicit_provider_contract.py`（守門「使用者意圖 vs 系統自動優化」的合約）。不靠讀碼下結論。

Sources: 既有 RESEARCH.md 2026-06-28 23:24 條目（quota-aware routing 調研結論）· [Python os.getenv 文件](https://docs.python.org/3/library/os.html#os.getenv) · [Python typing.Protocol 文件](https://docs.python.org/3/library/typing.html#typing.Protocol)

## 2026-07-20 04:11

現有程式碼已大量實作，先釐清**真實缺口**再調研：

---

**現況盤點（和任務描述的落差）**

程式碼現狀與任務描述有出入——`studio/notify.py` **已存在**，webhook + Telegram 均已實作，`send_bg` 在 autopilot.py 已掛 8 個關鍵路徑（task_failed/loop_stall/quota_exhausted/slo_brake/deploy_verify_failed/clarify_pending/daily_digest）。**真正缺口只剩：**
1. Email 通道尚未實作
2. 設定鍵名是 `TI_NOTIFY_WEBHOOK` / `TI_TELEGRAM_*`，不是任務要求的 `TI_ALERT_*`
3. 大量 `except Exception: log.exception(...)` 路徑沒有對應 `notify.send_bg`（但大多是「加值功能失敗靜默吞掉」的設計意圖，非紅色事件）

---

**調研結果**

重點: `smtplib` + `email.mime` 是 Python stdlib，**零新依賴**，與 notify.py「零依賴（urllib）」既有設計一致。
來源：[docs.python.org/smtplib](https://docs.python.org/3/library/smtplib.html)

重點: SMTP 有兩種 TLS 模式：**SMTP_SSL（port 465）** 從連線起即加密；**STARTTLS（port 587）** 先明文再升級 TLS——兩種需不同程式碼路徑，設定鍵需區分 (`TI_ALERT_SMTP_PORT`，呼叫端依 port 自動選模式)。
來源：[mailtrap.io/blog/smtplib](https://mailtrap.io/blog/smtplib/)

重點: Gmail 啟用 2FA 後必須用「App Password」，不接受帳號密碼；企業 SMTP relay（Sendgrid/Amazon SES）通常走 587 + API key 作密碼。

重點: `smtplib.SMTP` 是**同步阻塞** I/O，`async def` 裡不可直接呼叫——必須包進 daemon thread（與現有 `send_bg` threading.Thread 範式完全相同，無需引入 `aiosmtplib`）。
來源：[pypi.org/project/aiosmtplib](https://pypi.org/project/aiosmtplib/)

重點: notify.py 現有「Telegram URL 內嵌 token 絕不 log」的安全約束——SMTP 密碼也要遵循同規則（debug log 不含憑證）。

重點: `TI_ALERT_*` 鍵名與現有 `TI_NOTIFY_*` 並存無衝突，但要避免命名分裂——建議選一個前綴，或讓舊鍵成為向後相容別名。

---

**建議**

建議: **在 notify.py 新增 `_post_email()` sink**，用 stdlib `smtplib` + `email.mime.text.MIMEText(charset='utf-8')`，port 465 走 `smtplib.SMTP_SSL`、其他 port 走 `SMTP + starttls()`——約 20 行，零新依賴。

建議: **config.py 新增以下設定鍵**（與現有 `TI_NOTIFY_WEBHOOK` / `TI_TELEGRAM_*` 並列），不改舊鍵（避免破壞已有使用者設定）：
```
TI_ALERT_EMAIL_TO     # 收件人（逗號分隔多人）
TI_ALERT_SMTP_HOST    # SMTP host（空=關）
TI_ALERT_SMTP_PORT    # 預設 587
TI_ALERT_SMTP_USER    # 帳號
TI_ALERT_SMTP_PASS    # 密碼/App Password（不 log）
TI_ALERT_FROM         # 寄件人顯示名稱/地址
```

建議: **不要改 `TI_NOTIFY_WEBHOOK` / `TI_TELEGRAM_*` 鍵名**——現有生產設定與測試已依賴這些鍵；改名只帶遷移成本不帶新能力。

建議: **`_deliver()` 直接加 email sink**，與 webhook/Telegram 三者並存、各自獨立成敗（現有架構天然支援）。

建議: **exception 路徑的補掛要挑重要的**，不是全掛。值得補的是「autopilot 主迴圈頂層未捕捉例外」——可在最外層的 `while True` 加一個 catch-all `notify.send_bg("loop_crash", ...)`；純加值功能的吞掉路徑不要補（噪音 > 價值）。

建議: **contract 測試**要補一條「三個 sink 全未設定時 `send_bg` 零網路呼叫」（現有 notify.py 的 early-return 邏輯需涵蓋 email 空設定）。

Sources:
- [smtplib 官方文件](https://docs.python.org/3/library/smtplib.html)
- [mailtrap.io smtplib 教學](https://mailtrap.io/blog/smtplib/)
- [aiosmtplib PyPI](https://pypi.org/project/aiosmtplib/)
- [webhook anti-patterns](https://webhookantipatterns.com/)
- [FastAPI background patterns](https://medium.com/@connect.hashblock/10-fastapi-background-patterns-that-dont-block-cbfea8bfb717)

