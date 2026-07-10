# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 驗證 `git status --porcelain` 與 `git diff` 皆為空，並確認 `tests/test_task1_verify_report_contract.py` 不存在於工作樹（含 untracked），貼命令輸出全文
- [ ] 收尾重跑 `git status --porcelain`＋`git worktree list`，確認無其他 lane 殘跡與 HEAD 漂移，貼輸出自證閉環
