# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 執行 `git fetch origin` 並確認本批 commit 已合併：記錄 `git rev-parse HEAD origin/main`、`git merge-base HEAD origin/main` 三值一致，且 `git status --porcelain` 乾淨
- [ ] 以 `git worktree add "$TMPDIR/ti-lane-task-1" HEAD` 建 task-1 身分 lane，在其中以主 repo `.venv/bin/python -m pytest tests/test_task1_retry_doc.py -rA -q` 重跑，取得 `test_no_py_changed` PASSED 且整檔全綠，跑完 `git worktree remove --force` 並以 `git worktree list` 證無殘留
- [ ] 撰寫權威記錄檔 `.ci-evidence/task1-no-py-changed-rerun-report.md`：含合併基準三 SHA、兩 lane 對照結果（skip 全文＋PASSED 輸出摘要）、實跑命令原文、「本檔為唯一權威、其餘中間產物應忽略」聲明
