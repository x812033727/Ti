# 開發指南（Contributing）

歡迎參與 Ti Studio。本文件說明本地開發環境、品質工具與提交慣例，並作為
**dev 指令（安裝／測試／lint／pre-commit）的唯一權威來源**——其他文件僅以敘述或連結引用，不再重複可複製的指令區塊。

## 環境建置

需要 Python 3.10+。本文件指令以 Linux/macOS 為準；Windows 請將 `.venv/bin/python` 改為 `.venv\Scripts\python`。

建 venv 階段尚無 `.venv`，只能用系統 Python，故用 `python3`；建好後一律走 venv 內直譯器 `.venv/bin/python`。

```bash
python3 -m venv .venv                                   # 建 venv（此階段尚無 .venv，故用系統 python3）
.venv/bin/python -m pip install -e ".[dev]"             # 安裝套件 + 開發工具（pytest / ruff / pre-commit）
cp .env.example .env                                     # 視需要填入金鑰或門禁密碼
.venv/bin/python -m pre_commit install                  # 裝 git hook，提交前自動 lint / 格式化
```

> 跑測試與離線示範**不需** API 金鑰；只有真正要驅動 LLM 專家時才需要
> `ANTHROPIC_API_KEY`（或 OpenAI 設定）。

## 日常開發

```bash
# 啟動（離線示範，免金鑰，最快看到完整流程）
TI_OFFLINE=1 .venv/bin/python -m studio.server

# 跑全部測試
.venv/bin/python -m pytest -q

# Lint 與格式化
.venv/bin/python -m ruff check .            # 檢查
.venv/bin/python -m ruff check . --fix      # 自動修正可修的問題
.venv/bin/python -m ruff format .           # 套用格式
.venv/bin/python -m ruff format --check .   # 只檢查（CI 用）
```

CI（GitHub Actions）會在每次 push / PR 跑兩個 job：

- **lint**：`ruff check` + `ruff format --check`
- **test**：在 Python 3.10 / 3.11 / 3.12 矩陣上跑 `pytest`（含覆蓋率）

請在送出前確認本地 `ruff` 與 `pytest` 皆通過。

## shell 用法安全掃描

為偵測潛在的 shell 注入面，repo 內有一支共用掃描腳本
`scripts/scan_shell_usage.sh`，會掃出兩類用法：

- `subprocess.run(..., shell=True)` 等 → 由 Ruff `S602/S604/S605` 命中
- `asyncio.create_subprocess_shell(...)` → 由 ripgrep/grep 補掃命中
  （Ruff S 規則不抓它，因它「天生就是 shell」、無 `shell=` 參數）

這支腳本是**唯一事實來源（SSOT）**：CI 的 lint job、pre-commit hook、本機 demo
三處都只呼叫它，規則與參數天然一致。

### 本機重現（與 CI 一致）

```bash
# (a) 與 CI lint job 的「Scan shell usage」step 完全相同的指令；預設掃 studio/
bash scripts/scan_shell_usage.sh

# (b) 同時展示兩類命中（S602 + create_subprocess_shell）——掃測試樣本
bash scripts/scan_shell_usage.sh tests/fixtures
```

> 上面 (a) 的輸出與 CI lint job 的 **Scan shell usage** step 一致（同掃 `studio/`、
> 同 `SCAN_MODE=warn`），可直接對照。`tests/fixtures/shell_usage_sample.py`
> 是刻意留存的命中樣本，故 (b) 必然各出現一筆兩類命中。

### 目前為 warning-only

掃描現階段**只警告、不阻斷**：

- CI step 設 `continue-on-error: true`，且腳本在 `warn` 模式恆回傳 0。
- pre-commit hook 同樣不阻斷 `git commit`，僅印出警告。

### 升級為 blocking（單步操作）

確認既有命中都清乾淨後，把模式改成 `block`，腳本在有命中時即回非零：

```bash
SCAN_MODE=block bash scripts/scan_shell_usage.sh
```

正式升級時，只需在 CI step 的 `env` 與（如需）pre-commit hook 將
`SCAN_MODE` 設為 `block` 即可——單一槓桿，三處邏輯不變。

## 程式風格

- 規則與行長集中在 `pyproject.toml` 的 `[tool.ruff]`，請勿在個別檔案覆寫。
- 沿用既有風格：中文註解、簡潔的 docstring、`from __future__ import annotations`。
- 不隨意新增依賴；認證等功能優先用標準庫。

## 測試慣例

- 測試放在 `tests/`，檔名 `test_*.py`，用 `pytest`（`asyncio_mode = "auto"`）。
- 端到端流程請走離線假專家（見 `tests/test_offline_e2e.py`），避免測試依賴外部 API。
- 新增後端能力時，盡量補上對應測試（例如 `tests/test_auth.py` 之於門禁）。

## 分支與提交

- 從 `main` 開功能分支：`feat/...`、`fix/...`、`docs/...`。
- commit 訊息用祈使句、聚焦單一變更；可用中文。
- PR 前的檢查清單：
  - [ ] `ruff check .` 與 `ruff format --check .` 通過
  - [ ] `.venv/bin/python -m pytest -q` 全綠
  - [ ] 必要時更新 `README.md` / `ARCHITECTURE.md` / `.env.example`

## 進一步閱讀

模組地圖、事件流與認證流程請見 [ARCHITECTURE.md](ARCHITECTURE.md)。
