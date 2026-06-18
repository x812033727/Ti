# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 在 `_run_codex()` 結束路徑用 `finally` 清理 `self._proc`，且只在 `self._proc is proc` 時清理
