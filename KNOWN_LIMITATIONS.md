# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 給 `orchestrator._teardown_lane` 補整段 `asyncio.timeout` 防爆閥（模組常數）＋進入前 broadcast phase_change 錨點＋expert stop 改 `asyncio.gather` 並行，並補「stop 永不返回」時 teardown 有界收斂測試
- [ ] 稽核「全部 lane 收斂→final demo」間其餘非 LLM await 的 timeout 覆蓋（`_integrate_wave` 的 git 合併/flush/snapshot、demo 前置），缺口補 timeout、無缺口以程式註解載明依據
