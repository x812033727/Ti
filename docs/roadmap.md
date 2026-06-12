# 補強 Roadmap（產品藍圖層之後的三個階段）

目標：「說出要做什麼產品 → 團隊開始討論、開始研究、越做越進步」。
第一階段「產品藍圖層」（藍圖/ADR/backlog 優先級）已落地；以下為後續階段的待辦移交，
實作時各自開任務、帶測試。

## 階段二：研究能力

1. ✅ **實作中即時研究（已落地）**：`TI_RESEARCH_TOOLS`（opt-in）讓 ENGINEER/SENIOR 的
   `allowed_tools` 附加 `WebSearch`/`WebFetch`（`roles.effective_tools`）。Claude 路徑由 SDK
   原生支援；OpenAI function-calling 路徑由 `studio/tools.py` 的 `web_fetch` 工具承接
   （httpx 抓取＋剝 HTML 摘要、輸出截斷）。
2. ✅ **研究網域白名單（已落地）**：`TI_RESEARCH_ALLOWED_DOMAINS` 控管研究流量；SSRF 防護
   `tools.research_url_check`（scheme 限 http/https、私網/loopback/link-local 位址永遠擋、
   DNS 解析後逐位址重驗、redirect 逐跳重驗）同時施加於 OpenAI 路徑（web_fetch）與 Claude
   路徑（`experts._auto_allow_tool` 攔 WebFetch）；逾時/無網路降級為「無調研續行」。
3. **可行性評估階段（待辦）**：`_architecture_decision` 前插一輪 Researcher 對候選方案的
   `重點:/建議:` 評估，結論餵入 ADR（與 `adr.record` 串接）。
4. 接點：`studio/roles.py`、`studio/tools.py`、`studio/providers.py`、`studio/experts.py`、
   `studio/config.py`、`studio/settings.py`。

## 階段三：越做越進步迴圈

1. **教訓庫語意去重與蒸餾**：`lessons.distill()`——定期用一次 LLM 把相近教訓合併、淘汰
   過時項（取代全文相符去重＋FIFO 截斷）；教訓加 `scope`（global/project）與使用計數。
2. **記分卡回饋進流程**：`history._derive_scorecard` 按專案聚合（demo 通過率/退回率），
   注入 improver 的「找問題」與 `_compose_requirement` 前綴（擴充 `_recent_outcomes_context`），
   讓找問題對準弱項。
3. **跨專案知識/模板**：高頻 ADR/藍圖模式蒸餾成全域模板庫，新專案藍圖生成時注入同類型
   產品的既有結論。
4. 接點：`studio/lessons.py`、`studio/history.py`、`studio/improver.py`、`studio/memory.py`。

## 階段四：驗證能力

1. **長駐服務 Demo**：`runner.run_service()`——啟動（新進程組）→ 輪詢健康檢查 → HTTP 打點
   → SIGTERM 收殮（殺進程組邏輯已有）；PM/工程師宣告 `服務指令:`/`健康檢查:` 行標記，
   `_final_demo` 依宣告選擇單次執行或服務模式。
2. **API 黑箱測試**：QA 對宣告端點做請求/斷言，結果納入客觀閘門（`OBJECTIVE_GATE`）。
3. **沙箱取捨**：`TI_SANDBOX_NET=0` 下 bwrap 隔離 netns 仍有 loopback——把「啟動＋打點」包進
   **同一個** bwrap 調用（單一腳本），避免為驗證而全域開網。
4. **離線對應**：`fake_experts.py` 補最小 HTTP 服務示範情境，讓 E2E 覆蓋服務型 demo。
5. 接點：`studio/runner.py`、`studio/orchestrator.py`、`studio/config.py`、`studio/fake_experts.py`。
