# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 在專案 .venv（ruff 0.14.4）重跑 `ruff format --check tests/`，貼出 exit 0 與指名 7 支檔「already formatted」輸出；唯有非 0 才跑一次 `ruff format tests/` 收尾並提交
- [ ] 在模擬無 SDK 環境（`sys.modules['claude_agent_sdk']=None`）跑 `pytest --collect-only -q tests/` 確認 0 collection error，並 grep 證明 tests/ 無頂層 `import claude_agent_sdk`
- [ ] 稽核 `_build_discovery_prompt` 主動分散層與 `_filter_pending_duplicates` 兩道防線是否到位且比對範圍對齊；有缺口才補，且不改 `backlog._is_duplicate` 字串等值契約
