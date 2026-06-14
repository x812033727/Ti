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

# --- 測試隔離（hermetic）：對齊乾淨 CI，隔絕執行環境殘留 ------------------------
# studio.config 於 import 時讀 env 且會 load_dotenv()——python-dotenv 會從 config.py
# 所在目錄一路向上搜 .env（部署機上 /opt/ti/.env 會被搜到），TI_CRITIC=1、TI_NOTES=1
# 等部署值會翻轉 config 預設，讓「預設不啟用 X」類測試默默打掛。CI 乾淨無這些殘留，
# 此處在任何測試模組 import studio.config 之前做兩件事對齊：
#
# 1) 把 dotenv.load_dotenv 換成 no-op——config.py 是 `from dotenv import load_dotenv`，
#    在它 import 前改掉 dotenv 模組屬性即可攔下，祖先目錄的 .env 不再漏進測試。
#    （tests/settings 的子程序測試另起 process 跑真 server，不受此 in-process stub
#    影響；該測試自行備份/還原 .env。）
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False

# 2) 清掉殘留的 TI_* 環境變數（含 TI_DISCUSS_*）：殘留值會讓既有測試默默改道
#    （如 legacy ↔ engine 路徑翻轉、critic/notes 開關翻轉）。測試要改設定一律
#    `monkeypatch.setattr(config, "<欄位>", ...)` 改屬性，或 setenv 後 config.reload()。
#
#    例外——TI_SANDBOX*（TI_SANDBOX / TI_SANDBOX_NET / TI_SANDBOX_BWRAP…）必須保留：
#    它們描述「主機沙箱能力」而非行為偏好，是 CI sandbox-test job 刻意注入的
#    （ci.yml 設 TI_SANDBOX=1、TI_SANDBOX_NET=1；NET 預設 0，被清掉會讓 bwrap 走
#    --unshare-net，在 GitHub runner 觸發 RTM_NEWADDR EPERM → 沙箱測試全紅）。
for _k in [k for k in os.environ if k.startswith("TI_") and not k.startswith("TI_SANDBOX")]:
    os.environ.pop(_k, None)


# --- 向後相容:starlette 0.41 移除了 TestClient 的 `client=` 參數 ---------------
# 多個安全測試用 `TestClient(app, client=(ip, port))` 設定 ASGI scope 的 client peer，
# 模擬 loopback / 公網來源以驗證 require_loopback 門禁(netutil.is_loopback)。starlette 0.41
# 起 TestClient.__init__ 不再收 client(scope 寫死 ["testclient", 50000])——若 deploy 拉到
# 新版 starlette,~20 個測試會在 setup 就 TypeError。此處還原相容:有 client= 時包一層 ASGI
# app 覆寫 scope["client"],版本無關、純測試、不動生產相依。已有 client= 的舊版則不 patch。
import inspect as _inspect  # noqa: E402
import starlette.testclient as _sttc  # noqa: E402

if "client" not in _inspect.signature(_sttc.TestClient.__init__).parameters:
    _orig_tc_init = _sttc.TestClient.__init__

    def _tc_init_with_client(self, app, *args, client=None, **kwargs):
        if client is not None:
            _real_app = app

            async def _scope_client_app(scope, receive, send):
                if scope.get("type") in ("http", "websocket"):
                    scope = dict(scope)
                    scope["client"] = list(client)
                await _real_app(scope, receive, send)

            app = _scope_client_app
        _orig_tc_init(self, app, *args, **kwargs)

    _sttc.TestClient.__init__ = _tc_init_with_client
