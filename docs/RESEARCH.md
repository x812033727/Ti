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

