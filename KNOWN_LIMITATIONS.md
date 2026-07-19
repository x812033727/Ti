# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 依 #1 結果收斂：若有待格式化檔案則執行 `timeout 60 .venv/bin/python -m ruff format .` 並以單一 commit 提交（訊息含 ruff 0.14.4 與檔數）；若無則明確回報「零改動、不提交」，不得產生空 commit 或任何範圍外改動
- [ ] 獨立複驗收尾：實跑 `ruff format --check .` 與 `ruff check .` 皆 RC=0、`git status --porcelain` 空白，並核對 #2 宣稱（有 commit 則 `git log -1 --stat` 確認只含格式化改動；零改動則確認 HEAD 未變）
