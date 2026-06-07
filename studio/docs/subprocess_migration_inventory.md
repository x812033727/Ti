# `run_command` shell 呼叫端遷移清冊

基準範本：`publisher.py` 的 `_push`（已全面 argv 化）——固定指令拆成 `list[str]`、改呼叫
`run_command_exec`、帶固定 `label`、顯式帶齊 `timeout`/`sandbox`。

雙路徑並存：
- `run_command`（`runner.py:139`）→ `create_subprocess_shell`（`/bin/sh -c`，解析 metacharacter）
- `run_command_exec`（`runner.py:172`）→ `create_subprocess_exec`（argv 陣列，shell 不參與解析）

分類定義：
- **(a) 可直接 argv 化**：固定字串或已是 list、無 pipe/glob/`&&`/變數展開等 shell 語法 → 遷移到 `run_command_exec`。
- **(c) 須保留 shell**：指令含動態/使用者輸入，可能帶 shell 語法 → 維持 `run_command`，加註解說明。

---

## 清冊（涵蓋全部 5 處）

| # | 檔案:行 | 內容 | 分類 | 理由 | 遷移注意 |
|---|---|---|---|---|---|
| 1 | `runner.py:267-273` | `git init -q` / `git config user.email …` / `git config user.name 'Ti Studio'` / `git config commit.gpgsign false` 四行固定 git init/config | **a** | 全為固定字串，無任何動態輸入或 shell 語法 | 四行逐行手寫 argv 改 `run_command_exec`；各帶 `sandbox=False`、`timeout=20`（不得依賴預設 `sandbox=None`，會走 fail-closed）；`user.name` 值改為 `"Ti Studio"`（去掉單引號，引號是 shell 產物，argv 不需要） |
| 2 | `runner.py:309` (`git_clone`) | `git clone --depth 1 [--branch <b>] <authed_url> .` | **a** | `parts` 本就是 list，目前只是 `shlex.quote`+`join` 又組回字串，等於白繞一圈 | 用 `parts + [authed, "."]` 直接組 argv，刪除 `shlex.quote`+`join`；`run_command_exec` 只帶固定 `label="git clone"`（嚴禁把含 token 的 `authed`/`cmd` 傳入 label）；`timeout=180, sandbox=False`；token 遮蔽（L312 `replace(token,"***")`）與 `result.command` 覆寫（L313）保持原順序、原位置不動 |
| 3 | `autopilot.py:85` (`_gate_tests`) | `python -m pytest -q` | **a** | 固定字串，無 shell 語法 | 改 argv `["python","-m","pytest","-q"]` 走 `run_command_exec`；保留 `timeout=600, sandbox=True`；PR2 須在 sandbox 下實跑驗證 bwrap 環境能解析到 `python` 執行檔（PATH 問題） |
| 4 | `orchestrator.py:718` (`_self_test`) | `run_command(self.cwd, cmd)`，`cmd` 來自 `parse_run_command(impl_text)` 或 `resolve_demo_command(...)` | **c** | `cmd` 是 PM/工程師宣告的自測指令，動態解析而來，可能含 pipe/`&&`/glob 等 shell 語法 | 維持 `run_command`，加註解「動態/使用者指令需 shell 語法，刻意保留」；本體加 `# nosec B602` + 理由，避免納管後誤報擋 CI |
| 5 | `orchestrator.py:735` (`_final_demo`) | `run_command(self.cwd, cmd)`，`cmd` 來自 `resolve_demo_command(...)` | **c** | 同上，demo 指令動態解析，可能含 shell 語法 | 同 #4 |
| 6 | `tools.py:131` (`run_bash` 工具) | `run_command(cwd, args.get("command", ""))` | **c** | `command` 為工具呼叫端任意輸入，本就是要當 bash 執行 | 維持 `run_command`，加註解 + `# nosec B602` |

> 註：表列 6 列對應「5 處呼叫端」——`orchestrator.py:718/735` 為同一類 demo/自測指令的兩個進入點，計為一處（#4/#5）。其餘 runner git init/config 四行（#1）合計為一處。

---

## 遷移結論

- **分類 a（遷移）**：runner.py git init/config（#1）、git_clone（#2）、autopilot pytest（#3）。
- **分類 c（保留 shell + 註解 + `# nosec`）**：orchestrator.py 718/735（#4/#5）、tools.py 131（#6）。
- 全域複查指令：`grep -rn "run_command(" --include=*.py . | grep -v "run_command_exec\|def run_command\|parse_run_command"`——
  輸出應僅含上表 a/c 兩類呼叫端，無第三方遺漏。

## 分批 PR 對照

- **PR1**：本清冊 + CI/pre-commit 掃描骨架（先設 warning 非阻斷；須先實測 Bandit 能否命中 `asyncio.create_subprocess_shell`，抓不到改 ruff `S` 規則或 grep gate）。
- **PR2**：執行分類 a 遷移（#1/#2/#3）+ metacharacter 純文字測試 + git_clone 失敗路徑遮蔽回歸 + 分類 c 的 `# nosec` 標註齊全；sandbox 下實跑 pytest。
- **PR3**：將 CI 掃描由 warning 轉為阻斷。
