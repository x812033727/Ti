"""共用的 repo root 解析。

集中原本散落在 ~50 個測試中、各自重複的 `Path(__file__).resolve().parent.parent`。
本檔固定放在 tests/ 下，故 `.parent.parent` 永遠指向 repo root——即使未來把測試
按子系統移進 tests/<subsystem>/ 子目錄，REPO_ROOT 仍正確（搬動的是引用方，不是本檔）。

tests/conftest.py 會把本目錄加入 sys.path，使子目錄中的測試也能 `from _repo import REPO_ROOT`。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
