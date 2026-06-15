# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 在當前 HEAD 重跑 ruff、pytest --collect-only、pytest 三者，確認哪些已拆/未拆並產出基準快照
- [ ] 在 orchestrator 三閘門（lint/collect/test）的回報與 backlog note 加上明確 `[lint]`/`[collect]`/`[test]` 層級標籤
