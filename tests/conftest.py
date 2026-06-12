"""讓 tests/ 下（含未來的子目錄）的測試都能 import 同層輔助模組（如 `_repo`）。

pytest 在 prepend import 模式下只會把「測試檔所在目錄」加入 sys.path；一旦把測試
移進 tests/<subsystem>/ 子目錄，`from _repo import REPO_ROOT` 就會找不到 tests/ 下的
_repo。此 conftest 由 pytest 在整個 tests 樹收集前自動載入，先把 tests/ 釘上 sys.path，
讓共用輔助模組無論測試位於哪一層都可被 import。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 測試隔離：清掉開發機可能殘留的 TI_DISCUSS_* 環境變數——studio.config 於 import 時讀 env，
# 殘留值會讓全部既有測試默默改道（legacy ↔ engine 路徑翻轉）。conftest 在任何測試模組
# import studio.config 之前載入，於此清除最保險。測試要驗分流時一律
# `monkeypatch.setattr(config, "DISCUSS_MODE", ...)` 改屬性，不用 setenv。
os.environ.pop("TI_DISCUSS_MODE", None)
os.environ.pop("TI_DISCUSS_MAX_ROUNDS", None)
