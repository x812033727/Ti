# Ti Studio — AI 專家討論工作室

一個由多位 AI 專家組成的自主軟體開發「工作室」。給它一段產品需求，工作室裡的
**專案經理、工程師、高級工程師、驗證工程師** 就會自己討論、寫程式、測試、審查、
反覆改進，最後做出可運行的成果 —— 整個過程會在網頁上即時呈現。

由 [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) 驅動，
專家們使用內建的 Read / Write / Edit / Bash 工具真的去寫檔案、執行程式。

## 工作流程

```
需求 → PM 拆解(任務+驗收標準) → 工程師實作 → 驗證工程師測試 → 高級工程師審查
        ↑__________________ 未通過則退回改進(最多 3 輪) __________________|
                          → PM 驗收 → 團隊檢討 → 完成
```

## 角色

| 角色 | 職責 | 工具 |
|------|------|------|
| 🧭 專案經理 | 拆解需求、定驗收標準、判斷完成、主持檢討 | 唯讀 |
| 👩‍💻 工程師 | 實際撰寫與修改程式碼 | Read/Write/Edit/Bash |
| 🔬 驗證工程師 | 撰寫並執行測試、回報結果 | Read/Write/Edit/Bash |
| 🧠 高級工程師 | 審查品質/設計/安全、核可或退回 | 唯讀 + Bash |

## 安裝

需要 Python 3.10+ 與 [Claude Code](https://code.claude.com) 執行環境。

```bash
pip install -e .            # 或：pip install claude-agent-sdk fastapi "uvicorn[standard]" python-dotenv
cp .env.example .env        # 填入 ANTHROPIC_API_KEY
```

## 啟動

```bash
export ANTHROPIC_API_KEY=sk-...      # 或寫在 .env
python -m studio.server              # 或：uvicorn studio.server:app
```

開啟瀏覽器 http://localhost:8000 ，輸入需求（例如「做一個能計算 BMI 並分類的 Python CLI」），
按「開始討論」即可觀看專家協作。產出的程式碼會放在 `workspaces/<session_id>/`。

## 設定

可用環境變數（見 `.env.example`）調整：模型（`TI_MODEL_LEAD` / `TI_MODEL_FAST`）、
最大改進輪數（`TI_MAX_ROUNDS`）、伺服器位址（`TI_HOST` / `TI_PORT`）。

## 測試

不需 API 金鑰的流程狀態機單元測試：

```bash
pip install -e ".[dev]"
pytest
```

## 架構

```
studio/
  config.py        設定（模型、輪數、路徑、伺服器）
  roles.py         四位專家的角色與 system prompt
  events.py        StudioEvent 事件（WebSocket 傳輸）
  workspace.py     每個 session 的沙箱工作目錄
  experts.py       Expert：包裝 ClaudeSDKClient，串流回應轉事件
  orchestrator.py  StudioSession：討論/工作流程狀態機（核心）
  server.py        FastAPI + WebSocket + 靜態檔
web/               免建置的工作室前端（HTML/CSS/JS）
tests/             以 stub 專家測試狀態機
```

## 後續可擴充

逐任務迭代多個任務、人類可在討論中插話介入、產出歷史存檔與重播、可切換多家 LLM、
把成果自動 commit 到 git。
