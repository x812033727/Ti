# `run_command` shell 呼叫端遷移清冊

## 行號守門

- 類型：`marker-only`
- 狀態：`not-required`
- 守門測試：不適用
- 模板：`studio/docs/inventory_line_guard_convention.md`
- 原則：本檔以 `檔案.py::函式名` 錨點定位，不把行號當契約；若加入現碼行號，需改用 `line-number` 並補正式命名守門測試。

基準範本：`publisher.py` 的 `_push`（已全面 argv 化）——固定指令拆成 `list[str]`、改呼叫
`run_command_exec`、帶固定 `label`、顯式帶齊 `timeout`/`sandbox`。

雙路徑並存：
- `run_command`（`runner.py::run_command`）→ `create_subprocess_shell`（`/bin/sh -c`，解析 metacharacter）
- `run_command_exec`（`runner.py::run_command_exec`）→ `create_subprocess_exec`（argv 陣列，shell 不參與解析）

分類定義：
- **(a) 可直接 argv 化**：固定字串或已是 list、無 pipe/glob/`&&`/變數展開等 shell 語法 → 遷移到 `run_command_exec`。
- **(c) 須保留 shell**：指令含動態/使用者輸入，可能帶 shell 語法 → 維持 `run_command`，加註解說明。

---

## 清冊（涵蓋全部 5 處）

> 定位採**函式錨點**（`檔案.py::函式名`）而非絕對行號——任何在呼叫端之前插入的
> 無關程式碼都不會位移錨點（見 issue #78）。**請勿把行號加回此欄**，否則動到
> orchestrator/runner 上半部的無關 PR 會再次反复誤觸 CI。驗收測試
> `tests/sandbox/test_qa_task1_subprocess_inventory.py` 以 AST 比對錨點。

| # | 檔案::錨點 | 內容 | 分類 | 理由 | 遷移注意 |
|---|---|---|---|---|---|
| 1 | `runner.py::git_init` | `git init -q` / `git config user.email …` / `git config user.name 'Ti Studio'` / `git config commit.gpgsign false` 四行固定 git init/config（**已遷移 exec**） | **a** | 全為固定字串，無任何動態輸入或 shell 語法 | ✅ 已遷移：四行逐行手寫 argv 改 `run_command_exec`；各帶 `sandbox=False`、`timeout=20`（不得依賴預設 `sandbox=None`，會走 fail-closed）；`user.name` 值改為 `"Ti Studio"`（去掉單引號，引號是 shell 產物，argv 不需要） |
| 2 | `runner.py::git_clone` | `git clone --depth 1 [--branch <b>] <authed_url> .`（**已遷移 exec**） | **a** | `parts` 本就是 list，原先只是 `shlex.quote`+`join` 又組回字串，等於白繞一圈 | ✅ 已遷移：用 `parts + [authed, "."]` 直接組 argv，刪除 `shlex.quote`+`join`；`run_command_exec` 只帶固定 `label="git clone"`（嚴禁把含 token 的 `authed`/`cmd` 傳入 label）；`timeout=180, sandbox=False`；token 遮蔽（`replace(token,"***")`）與 `result.command` 覆寫保持原順序、原位置不動 |
| 3 | `autopilot.py::_gate_tests` | `python -m pytest -q`（**已遷移 exec**） | **a** | 固定字串，無 shell 語法 | ✅ 已遷移：argv `[sys.executable,"-m","pytest","-q"]` 走 `run_command_exec`，`label="pytest gate"`；保留 `timeout=600, sandbox=True`。⚠️ 用 `sys.executable` 而非裸 `"python"`：多數環境（含本 CI）PATH 僅有 `python3`，裸 `python` 在 exec/sandbox 下會 `execvp: No such file` ——sys.executable 落實設計「避免 PATH 問題」意圖（已 sandbox 實跑驗證） |
| 4 | `orchestrator.py::_self_test` | `run_command(ctx.cwd, cmd)`，`cmd` 來自 `parse_run_command(impl_text)` 或 `resolve_demo_command(...)` | **c** | `cmd` 是 PM/工程師宣告的自測指令，動態解析而來，可能含 pipe/`&&`/glob 等 shell 語法 | ✅ 已標註：維持 `run_command`，加說明註解「刻意保留 shell」+ 行尾 `# nosec B602` |
| 5 | `orchestrator.py::_final_demo` | `run_command(self.cwd, cmd)`，`cmd` 來自 `resolve_demo_command(...)` | **c** | 同上，demo 指令動態解析，可能含 shell 語法 | ✅ 已標註：同 #4 |
| 6 | `tools.py::execute` （`run_bash` 分支） | `run_command(cwd, args.get("command", ""))` | **c** | `command` 為工具呼叫端任意輸入，本就是要當 bash 執行 | ✅ 已標註：維持 `run_command`，加說明註解 + `# nosec B602` |

> 註：表列 6 列對應「5 處呼叫端」——`orchestrator.py::_self_test` / `::_final_demo` 為同一類 demo/自測指令的兩個進入點，計為一處（#4/#5）。其餘 runner git init/config 四行（#1）合計為一處。

---

## 遷移結論

- **分類 a（遷移）**：runner.py git init/config（#1）、git_clone（#2）、autopilot pytest（#3）。
- **分類 c（保留 shell + 註解 + `# nosec`）**：`orchestrator.py::_self_test` / `::_final_demo`（#4/#5）、`tools.py::execute`（`run_bash` 分支，#6）。
- 全域複查指令：`grep -rn "run_command(" --include=*.py . | grep -v "run_command_exec\|def run_command\|parse_run_command"`——
  輸出應僅含上表 a/c 兩類呼叫端，無第三方遺漏。

## 分批 PR 對照

- **PR1**：本清冊 + CI/pre-commit 掃描骨架（先設 warning 非阻斷；須先實測 Bandit 能否命中 `asyncio.create_subprocess_shell`，抓不到改 ruff `S` 規則或 grep gate）。
- **PR2**：執行分類 a 遷移（#1/#2/#3）+ metacharacter 純文字測試 + git_clone 失敗路徑遮蔽回歸 + 分類 c 的 `# nosec` 標註齊全；sandbox 下實跑 pytest。
- **PR3**：將 CI 掃描由 warning 轉為阻斷。
