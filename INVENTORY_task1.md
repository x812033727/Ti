# 任務 #1 盤點：全 repo `python` 命中分類（change / keep）

**Oracle** = 驗收標準 #3 的 grep（`grep -rnE '\bpython\b' README.md CHANGELOG.md ARCHITECTURE.md CONTRIBUTING.md | grep -vE 'python3|\.venv|Scripts|python-|requires-python|setup-python|/python\b'`）；改後重跑須「零殘留」。本檔為 #2/#5 雙重驗收基準，工程師不得繞過 oracle 自行判斷。
**環境**：本機 `python3` 3.12.3、`python` symlink 不存在；目標環境＝未裝 python symlink 的 bare macOS Monterey+／Ubuntu 22.04+。
**本分支護欄**：`test_no_py_changed` 禁改任何 `.py` → 所有 `.py` 內命中一律 KEEP（硬約束），僅文件／`.sh` 可動。

---

## A. CHANGE 清單

### A1. 使用者面文件裸 `python <指令>`（task #2 範圍，4 行）
| 檔案:行 | 原文片段 | 改為 |
|---|---|---|
| README.md:251 | `python main.py add 3 4`（demo 敘述） | `python3 main.py add 3 4` |
| CHANGELOG.md:26 | `python -m studio ...`（```bash 圍欄，之前段範例命令） | `python3 -m studio ...` |
| CHANGELOG.md:36 | `python -m studio ...`（```bash 圍欄，之後段範例命令） | `python3 -m studio ...` |
| ARCHITECTURE.md:20 | `python -m studio.server`（入口宣告） | `python3 -m studio.server` |

> 自證對應：上 4 行 **恰等於** oracle grep 現況輸出。CONTRIBUTING.md CHANGE 行數 = 0（全為 venv 路徑/散文）。
> ARCHITECTURE.md:20「維持不變」措辭指「入口未被移除」，非指字面值沒動，按表改即可、不需改措辭。

### A2. 範圍外但同根因的可執行腳本（建議 CHANGE，需 PM 裁範圍）
| 檔案:行 | 原文 | 分類 | 理由 |
|---|---|---|---|
| scripts/redeploy.sh:9 | `exec python -m studio.server` | **CHANGE（建議，對齊 serve.sh 慣例）** | git 追蹤、會實跑的後備重佈署腳本；**直接 exec、不經 runner `_executable_command`**，bare 環境必 command-not-found，與 README demo 同根因。`.sh` 非 `.py`，不受 doc-only 護欄阻擋。 |

- **不判 KEEP 的理由**：部署環境無任何保證有 `python` symlink（PEP 394，py2 已 EOL），與使用者 demo 同一風險。
- **建議修法對齊 `scripts/serve.sh` 慣例**（B2 已列為 KEEP 的可攜啟動入口）：改為 `exec bash "$(dirname "$0")/serve.sh"` 或至少明寫 `python3`，與專案「禁止裸 python 前綴、偵測 `command -v python3 || python`」的既定慣例一致，勿只硬換字串。
- **範圍說明**：task #2 明列範圍為四份文件，redeploy.sh 在其外。請 PM 裁決：①併入本批（一行、同根因，推薦）或 ②另開跟進。task #1 在此明確分類並表態，不讓它缺席（高工第 2 輪必修點）。

---

## B. KEEP 清單（禁動區，零改動）

### B1. 四份使用者面文件內的禁動命中
- **SDK URL**：README.md:9 `/agent-sdk/python`
- **散文／套件欄位名**：README.md:64,72（`requires-python`、「Python ≥ 3.11」）、CONTRIBUTING.md:10（建 venv 前的「系統 Python / python3」散文）
- **Windows 路徑**：README.md:66,107,143,342、CONTRIBUTING.md:8（`.venv\Scripts\python`）
- **venv 直譯器**：README.md:122（`.venv/bin/python3` ＋ Windows）、CONTRIBUTING.md:8,14,16,26,29,32,33,34,35,106,107（`.venv/bin/python`）

### B2. 四檔以外全 repo 命中（全 KEEP，按類）
- **`.py` 原始碼（本分支硬禁動，且執行層自保）**：
  - `studio/runner.py:374` `return f"python {entry}"`——輸出**會再過 `_executable_command()`**，PATH 無 `python` 時換成 `sys.executable`，**邏輯自保**；架構決策「不動 runner 邏輯」。其餘 `runner.py:35/109/163/370` 為註解/docstring。
  - `studio/autopilot.py:1,92`（docstring/註解，92 行明示用 `sys.executable`）、`studio/server.py:4`（docstring）、`studio/roles.py:67`（提示字串範例）、`studio/settings.py:395`（`docs.python.org` placeholder）、`studio/fake_experts.py:43,55,74,75,114,238,255`（fake expert demo 輸出字串，執行時同樣經 runner 自保）。
  - `tests/**`：guard 釘死字串與 fixture，全 KEEP。含 `tests/docs/test_readme_consistency.py:86`（釘死 Windows `.venv\Scripts\python -m studio.server`，必須保留）、`tests/core/test_runner.py:13/14/39/41/50/57`（測 `python`→執行檔映射，屬執行層）。受 #2/#3 牽動者交 **task #4** 同 batch、同 commit 處理（含若有比對 CHANGELOG 原始字串的測試）。
- **CI／release workflow（架構決策不動，setup-python 已提供 python）**：`.github/workflows/ci.yml`（27/73/89/92/99/178/180/207/218）、`publish-release.yml`（52/75）、`release-smoke.yml`（32/60）。
- **歷史紀錄／決策檔（改了製造不實紀錄）**：`NOTES.md`、`BASELINE_task1.md`、`CLOSURE_task4.md`、`DECISIONS.md`、`adr.json`、`docs/issues/0001-*.md`（含 ```python 圍欄）、`docs/issues/0002-*.md`（`python -m pytest` 為 issue 實跑紀錄）。
- **Shell 腳本（`git grep -n 'python' -- '*.sh'` 單獨複查，全 KEEP，除 A2 redeploy.sh）**：
  - `scripts/serve.sh:3,5,10,11`——**唯一可攜啟動入口**，`PY="$(command -v python3 || command -v python)"` 防禦式偵測（python3 優先），3/5 行為說明慣例的註解。**KEEP**，且為 redeploy.sh 與 task #3 執行宣告應對齊的範本。
  - `scripts/baseline_selftest.sh:18,28,31`、`tests/server/smoke_agenda_run.sh:14,27`——已是 `python3`，**KEEP**。
- **工具腳本訊息／註解（非使用者指令宣告）**：`scripts/publish_release.py:28`（註解）、`scripts/scan_bare_pytest.sh:6/23/73`（lint 工具友善指引訊息）。
- **其他**：`.env.example:109`（`docs.python.org` 範例 URL）、`studio/docs/dev_command_dedup_inventory.md`（另一工作線交付物）。
- **`.venv/**`**：直譯器實體路徑，掃描已排除。

---

## C. 裁決與結論
1. **CHANGELOG:26/36 歸 CHANGE**：在 ```bash 圍欄內屬可執行命令範例（使用者照抄一樣 command-not-found），非歷史散文；保持 oracle 乾淨不開特例。「歷史段落禁動區」僅指描述舊版行為的散文句／版本號。
2. **真因在 OS shell 層**：bare 環境無 `python` symlink。改文件＋改 `redeploy.sh` 命中真因，不動 runner `_executable_command`（已自保）。
3. **需動手項共 5 筆**：使用者面文件 4 筆（A1，task #2）＋ 可執行腳本 1 筆（A2 redeploy.sh，需 PM 裁範圍）。其餘全 repo 命中皆 KEEP。
4. **批次替換禁用無邊界 sed**：逐檔精確 Edit，避免誤擊 `python3.x`/`python-dotenv`/`requires-python`/`docs.python.org`。
5. **shebang 不納入**：現有腳本均 `#!/usr/bin/env bash`，無 python shebang。
6. **doc-only 護欄**：`test_no_py_changed` 禁改 `.py`；所有 `.py` 命中為 KEEP 硬約束。
7. **盤點方法（避免重蹈漏網）**：除 oracle grep（僅掃 `.md`）外，shell 腳本另以 `git grep -n 'python' -- '*.sh'` 單獨複查全量入表。⚠ 注意 oracle 的 `grep -vE 'python3...'` 會**整行隱藏含 `python3` 的防禦式寫法**（如 serve.sh 的 `command -v python3 || python`），故 `.sh` 不能只靠 oracle 過濾，必須獨立 `git grep` 確認——這是前兩輪 `serve.sh`/`redeploy.sh` 漏網的真因。
