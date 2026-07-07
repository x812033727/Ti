# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目已於本輪補齊:

- [x] 重跑證據 #1/#2 線上重驗指令（`gh release view v0.2.0 --json body,tagName,url`、REST 同義抓取、`env PYTHONPATH=. python3 scripts/check_release_body_structure.py`），逐項比對 `docs/evidence/` 兩檔既有勾稽值並保存原始輸出至 `docs/evidence/release-v0.2.0-online-reverify-2026-07-07.json`；重驗工具為 `scripts/reverify_release_body.py`
