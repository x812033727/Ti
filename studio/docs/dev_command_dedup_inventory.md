# 任務 #1 盤點：README ↔ CONTRIBUTING dev 指令重複與測試約束

## 行號守門

- 類型：`historical-location`
- 狀態：`not-required`
- 守門測試：不適用
- 模板：`studio/docs/inventory_line_guard_convention.md`
- 原則：本檔的 `L<line>` 是收斂前盤點位置，不作現碼行號契約；若改成現碼行號 inventory，需改用 `line-number` 並補正式命名守門測試。

> 收斂方案：**單一權威 + 連結引用**，canonical = `CONTRIBUTING.md`。
> 本文僅為盤點（task #1），不改任何指令；供 task #2/#3/#4 落地依據。

## 1. 重複的指令區塊

| 指令 | README（canonical 前） | CONTRIBUTING（canonical 後） | 處置 |
|---|---|---|---|
| `pip install -e ".[dev]"` | `## 測試` L266（`.venv/bin/python3 -m pip install -e ".[dev]"`） | 環境建置 L13（裸 `pip install -e ".[dev]"`） | README 移除；CONTRIBUTING 保留為權威 |
| 跑測試 `pytest` | `## 測試` L267（`.venv/bin/python3 -m pytest`） | 日常開發 L28、PR checklist L106（`.venv/bin/python -m pytest -q`） | README 移除；CONTRIBUTING 保留 |
| `ruff check/format` | `## 測試` L268 | 日常開發 L31-34 | README 移除；CONTRIBUTING 保留 |
| `pre-commit install` | `## 測試` L269（`.venv/bin/python3 -m pre_commit install`） | 環境建置 L15（`pre-commit install`） | README 移除；CONTRIBUTING 保留 |

README 重複的「可複製執行區塊」= `README.md` L264-269（`## 測試` 段的 code block）。

## 2. Canonical 與引用點

- **Canonical（唯一可複製執行區塊）**：`CONTRIBUTING.md`
  - 環境建置 code block L11-16
  - 日常開發 code block L23-35
  - PR checklist L105-106
- **引用點（改後僅敘述 + 連結，不得有等價 code block）**：
  - `README.md` `## 測試` 段 L260-272 → 收斂為 2-3 行摘要 + `[CONTRIBUTING.md](CONTRIBUTING.md)`
  - `README.md` L271-272 已有指向 CONTRIBUTING / ARCHITECTURE 的連結（保留）

## 3. 受影響的 docs 測試斷言（硬約束）

| 測試檔 | 斷言 | 對收斂的約束 |
|---|---|---|
| `test_docs_pytest_command.py::test_readme_no_bare_pytest_command` | `^\s*pytest(\s\|$)` 不得命中 README | README 摘要句**不可**以 `pytest` 開頭；用「以 pytest 執行…」句式 |
| 同檔 `::test_contributing_pytest_prefix` | `\.venv/bin/python -m pytest -q` **≥2 處**；且無 `(?<![\w./-])python -m pytest` | CONTRIBUTING 須保留 ≥2 處（現 L28、L106 共 2 處）；勿引入裸 `python -m pytest` |
| 同檔 `::test_contributing_venv_python3` | 須含 `python3 -m venv .venv`；無 `(?<![\w3])python -m venv` | 保留 L12，勿改成 `python -m venv` |
| 同檔 `::test_all_pytest_run_commands_prefixed` | README+CONTRIBUTING 皆無裸 `python -m pytest` / 行首 `pytest` | 全檔約束，非僅測試段 |
| 同檔 `::test_windows_cross_platform_noted` | 須含 `.venv\Scripts` 或 Linux/macOS/Windows 字樣 | 保留 CONTRIBUTING L7 跨平台註記 |
| 同檔 `::test_inventory_untouched` | `subprocess_migration_inventory.md` git status 須乾淨 | **勿動** inventory 檔 |
| 同檔 `::test_venv_python_exists_and_runs` | `.venv/bin/python --version` 可跑（否則 skip） | 驗證前先建好 `.venv` 免誤判 |
| `test_readme_consistency.py`（全 7 條） | README code block 啟動/安裝/uvicorn 一律 `.venv` 完整路徑；`## 安裝` 段引用前置段；`切換到 OpenAI` 段須含特定 `.venv/bin/python3 ...` 字串 | 收斂只動 `## 測試` 段，**勿動**安裝/OpenAI/啟動段 |
| `test_qa_task3_precommit_step.py` | README「執行環境前置」happy-path pre-commit 步驟不動 | onboarding，非收斂目標 |
| `test_readme_verify_cmd.py` | README 驗證指令 + 預期 `ok` 保留 | 勿動驗證指令段 |
| `test_qa_task6_docs.py` | README 環境變數表 `TI_AUTOPILOT_*` 行（`next()` 抓行） | 與測試段無關，勿誤刪環境變數表 |

## 4. 給 task #2/#3 的落地提醒

1. README `## 測試` 段移除 L264-269 整個 code block，改 2-3 行摘要 + 連結；摘要句勿以 `pytest` 開頭。
2. CONTRIBUTING 維持 ≥2 處 `.venv/bin/python -m pytest -q`（現況已 2 處，安全落點：日常開發 L28 + PR checklist L106）。
3. 交付前驗證：`grep -c '\.venv/bin/python -m pytest -q' CONTRIBUTING.md` ≥ 2；`.venv/bin/python -m pytest tests/docs -q` 全綠。

## 5. baseline 現況

- docs 測試：**139 passed**（收斂前全綠）。
- CONTRIBUTING `.venv/bin/python -m pytest -q`：**2 處**。
- README 行首裸 `pytest`：**0**。
