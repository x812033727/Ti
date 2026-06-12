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

