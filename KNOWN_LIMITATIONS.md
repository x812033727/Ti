# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 撰寫 `tests/qa_test_verification_warning_contract.py`：黑盒跑 `bash scripts/verify-clean.sh`，斷言 WARN_FILE 警告分流契約（stdout 宣告 WARN_FILE 路徑、stderr/警告落 WARN_FILE 而非 stdout），每個 parametrize 配 ≥1 條負樣斷言
