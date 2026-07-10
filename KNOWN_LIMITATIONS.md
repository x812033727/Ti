# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 修改三處 fetch argv 為 force refspec：`studio/autodeploy.py:60`、`studio/deploy.py:159`、`studio/autopilot.py:2324` 均改為 `["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]`，不動其他邏輯
- [ ] 執行驗收閉環：跑全套測試與 ruff check/format --check 均綠，確認 `git status --porcelain` 之 diff 僅含 #1 三檔與 #2 測試檔，並確認 `repo_base.py`、`autopilot.py:120` 未被動到
