# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 確認 PR #398 CI 綠且無衝突後執行合併，使 550799cc 的黑樣本強化入 main；若 CI 紅或有衝突則回報阻塞原因、不強推
- [ ] 在 main（fetch 後）驗收：`tests/deploy/test_fetch_force_refspec.py` 存在且含 helper 與黑樣本測試，`timeout 300 .venv/bin/python -m pytest tests/deploy/ -q` 綠、`.venv/bin/python -m ruff check .` 綠、porcelain 空
