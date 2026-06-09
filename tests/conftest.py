"""讓 tests/ 下（含未來的子目錄）的測試都能 import 同層輔助模組（如 `_repo`）。

pytest 在 prepend import 模式下只會把「測試檔所在目錄」加入 sys.path；一旦把測試
移進 tests/<subsystem>/ 子目錄，`from _repo import REPO_ROOT` 就會找不到 tests/ 下的
_repo。此 conftest 由 pytest 在整個 tests 樹收集前自動載入，先把 tests/ 釘上 sys.path，
讓共用輔助模組無論測試位於哪一層都可被 import。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
