# 開發指南（Contributing）

歡迎參與 Ti Studio。本文件說明本地開發環境、品質工具與提交慣例。

## 環境建置

需要 Python 3.10+。

```bash
python -m venv .venv && source .venv/bin/activate   # 選填
pip install -e ".[dev]"     # 安裝套件 + 開發工具（pytest / ruff / pre-commit）
cp .env.example .env        # 視需要填入金鑰或門禁密碼
pre-commit install          # 裝 git hook，提交前自動 lint / 格式化
```

> 跑測試與離線示範**不需** API 金鑰；只有真正要驅動 LLM 專家時才需要
> `ANTHROPIC_API_KEY`（或 OpenAI 設定）。

## 日常開發

```bash
# 啟動（離線示範，免金鑰，最快看到完整流程）
TI_OFFLINE=1 python -m studio.server

# 跑測試
python -m pytest -q

# Lint 與格式化
ruff check .            # 檢查
ruff check . --fix      # 自動修正可修的問題
ruff format .           # 套用格式
ruff format --check .   # 只檢查（CI 用）
```

CI（GitHub Actions）會在每次 push / PR 跑兩個 job：

- **lint**：`ruff check` + `ruff format --check`
- **test**：在 Python 3.10 / 3.11 / 3.12 矩陣上跑 `pytest`（含覆蓋率）

請在送出前確認本地 `ruff` 與 `pytest` 皆通過。

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
  - [ ] `python -m pytest -q` 全綠
  - [ ] 必要時更新 `README.md` / `ARCHITECTURE.md` / `.env.example`

## 進一步閱讀

模組地圖、事件流與認證流程請見 [ARCHITECTURE.md](ARCHITECTURE.md)。
