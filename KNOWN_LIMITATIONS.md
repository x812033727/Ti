# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 盤點全 repo `python` 命中並分類為「必改（demo 執行的裸命令、文件範例）／不動（套件名、image tag、shebang、venv 內 `.venv/bin/python`、Windows `.venv\Scripts\python`）」，產出白名單清單
- [ ] 將必改清單中執行指令與文件裡的裸 `python`（如 `python main.py`、`python -m ...`）改為 `python3`
- [ ] 在 README/CONTRIBUTING 補一行 Windows 退路說明（`python3` 找不到時用 `py`），並文件化「venv 內允許 `python`、shell 範例統一 `python3`」慣例
- [ ] 全量重跑 `tests/docs` 與相關守護測試，確認文件一致性測試與既有測試皆綠
