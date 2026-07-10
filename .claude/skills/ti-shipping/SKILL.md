---
name: ti-shipping
description: Ti 出貨前自檢程序。何時用:實作完成準備提交/收尾前、lint 或測試閘門紅、CI 紅要修的時候。
---

# Ti 出貨前自檢

## 正確指令(一律用專案 venv,禁裸 python/pytest/ruff)

```bash
timeout 120 .venv/bin/python -m ruff check .
timeout 120 .venv/bin/python -m ruff format --check .
timeout 300 .venv/bin/python -m pytest -q
```

改了哪個子系統就先跑該子系統目錄省時間(tests/autopilot、tests/core、tests/server、tests/docs),
提交前仍須全套綠。

## 常見 CI 紅原因(依頻率)

1. **I001 import 排序 / 未用 import(F401)**:`ruff check --fix` 可自動修(safe),先修再看殘餘。
2. **格式漂移**:`ruff format .` 直接修;ruff 釘 0.14.4(pyproject/CI/pre-commit 三端同版,禁止升版)。
3. **E402(import 不在檔案頂部)**:autofix 修不掉,要手動把 import 移到頂部或加正當理由的 noqa。
4. **文件守門測試**(tests/docs):行號清單/env 覆蓋率/裸 python 掃描——新增 TI_ 旋鈕必須同步寫進
   `.env.example`;文件內引用的程式行號位移要同步更新。

## 改 config 旋鈕的鐵則

`studio/config.py` 有**兩處**要同步:頂層定義 + `reload()` 區塊內的重定義(約 1300 行起),
漏掉 reload 區塊=執行期改 env 不生效。布林慣例:`not in ("0", "false", "False", "")`。

## 提交訊息

繁體中文、首行說清楚「做了什麼+為什麼」;實質變更才提交,零 diff 就誠實回報 no-op。
