# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 將 #1/#2 重驗結果更新進 `docs/release-e2e-closure-report.md`：每列標「擷取日期＋本次線上重驗」、內嵌關鍵值與可照抄抽取指令；全 match 結論限定「閉環（僅及 v0.2.0）」，任一 mismatch 則寫缺口章＋結論降級，不修復、不動 evidence 檔、不動 marker 行
- [ ] 收尾驗收：跑 `.venv/bin/python -m pytest tests/docs -q` 全綠與 `ruff check`，確認 `git diff docs/` 僅 `docs/release-e2e-closure-report.md` 有預期改動、`docs/` 無 untracked 殘留
