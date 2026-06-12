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

