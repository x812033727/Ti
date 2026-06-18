# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 新增 `deploy-test` CI job，獨立執行 `python -m pytest tests/deploy -q`
- [ ] 設定 `deploy-test` 使用 Python 3.12、`TI_SANDBOX=0`、最小測試依賴與 `timeout-minutes: 10`
- [ ] 確保 `deploy-test` 沒有 path filter、沒有 `continue-on-error`、沒有 `--cov`
- [ ] 更新 README 的 branch protection / required checks 說明，補上 `deploy-test`
- [ ] 本地執行 deploy 測試並檢查 CI/README diff 僅限本需求範圍
