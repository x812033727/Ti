# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 重現基線並擷取因果證據——跑 `pytest --collect-only tests/autopilot`，確認當前 0 收集錯誤，並判定「未 re-export」是否為 7 模組失敗的完整單一因果
