# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 修改 tests/deploy/test_redeploy.py 的 autouse fixture，補 mock `deploy._deploy_lock`（contextmanager 假鎖 yield True）
- [ ] 修改 tests/deploy/test_redeploy_qa.py 的 autouse fixture，補 mock `deploy._deploy_lock`（同 #1 修法）
