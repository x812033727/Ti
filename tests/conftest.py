"""讓 tests/ 下（含未來的子目錄）的測試都能 import 同層輔助模組（如 `_repo`）。

pytest 在 prepend import 模式下只會把「測試檔所在目錄」加入 sys.path；一旦把測試
移進 tests/<subsystem>/ 子目錄，`from _repo import REPO_ROOT` 就會找不到 tests/ 下的
_repo。此 conftest 由 pytest 在整個 tests 樹收集前自動載入，先把 tests/ 釘上 sys.path，
讓共用輔助模組無論測試位於哪一層都可被 import。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

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

# state 安全寫入（secure_write_root）預設 strict＝寫入後驗證 owner 為 root（uid 0）。測試
# 環境（CI runner、本機）多以非 root 跑，strict 會讓 history/backlog 的每次寫入因 fchown
# 失敗而 raise，打掛大量既有測試。測試非 root 部署環境，故在此一律設 off（純原子寫入、
# 不驗 owner，行為等同安全機制導入前）；secure_write 專屬測試自行用 monkeypatch.setattr
# (config, "REQUIRE_CHOWN", ...)／require_chown= 參數 override 以驗證 strict/warn/off 三態。
os.environ["TI_REQUIRE_CHOWN"] = "off"

# 任務邊界部署自查（autopilot._maybe_boundary_redeploy）對整個測試樹關死：它會真的
# `git fetch`＋`deploy.redeploy()`（reset --hard + pip install + systemctl restart）——
# 任何未 stub deploy 的主迴圈測試若觸發，會把測試工作區 hard-reset、重啟真實服務
# （2026-07-10 實際發生過：開發工作區被自己的測試 reset 掉）。要測它的行為一律
# monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_CHECK_INTERVAL", ...) 顯式開。
os.environ["TI_AUTOPILOT_DEPLOY_CHECK_INTERVAL"] = "0"

# 調查任務分流輕量管線（autopilot._run_investigation_task）對整個測試樹關死：分類器是
# 寬鬆的標題正則，既有守護測試的假任務標題（「落檔測試」「收尾驗收：確認守門到位」…）
# 會被誤分流——lane 會真的建 Expert（SDK/LLM 呼叫），CI 無 SDK 直接炸、本機會打真模型。
# 要測 lane 一律 monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", True) 顯式開
# ＋ stub Expert（見 test_investigation_lane.py）。
os.environ["TI_AUTOPILOT_INVESTIGATION_LANE"] = "0"
# 原生 auto-merge 與 open PR reconciler（autopilot）對整個測試樹關死：兩者會打真實
# gh/GitHub API（pr merge --auto、pr view/list/close、_get_pr_status 的 httpx）——未 stub
# 的舊路徑測試會被快窗輪詢拖死、reconciler 更可能動到真實 repo 的 open PR。
# 要測它們一律 monkeypatch.setattr(config, "AUTOPILOT_AUTO_MERGE", True) 顯式開＋全面 stub。
os.environ["TI_AUTOPILOT_AUTO_MERGE"] = "0"

# 寫時 lint（studio/lint.py）對整個測試樹關死：它會對剛寫入的 .py 真跑 ruff 子程序——
# 既有工具測試的 fixture 檔會被自動格式化（實證：print(1) 被補換行害斷言紅）、拖慢全樹。
# 要測它一律 monkeypatch.setattr(config, "EXPERT_LINT_HOOK", True)（見 test_expert_lint_hook.py）。
os.environ["TI_EXPERT_LINT_HOOK"] = "0"


class ScopeRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir()
        self._git("init")
        self._git("config", "user.email", "scope-repo@example.test")
        self._git("config", "user.name", "Scope Repo")
        self._git("commit", "--allow-empty", "-m", "baseline")
        self.baseline = self._git("rev-parse", "HEAD").stdout.strip()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            check=True,
            text=True,
        )

    def write(self, path: str | Path, content: str = "") -> Path:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("scope_repo.write() expects a relative path inside the fixture repo")
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit(self, message: str = "change") -> str:
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def scope_repo(tmp_path: Path) -> ScopeRepo:
    return ScopeRepo(tmp_path / "scope-repo")


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


# --- 防 REQUIRE_CHOWN 跨測試洩漏（importlib.reload(config) 的副作用）-------------
# 多支測試以 setenv(TI_REQUIRE_CHOWN, ...) + importlib.reload(config) 驗三態解析，但
# monkeypatch 只還原 env、不還原已被 reload 重算的「模組級全域」config.REQUIRE_CHOWN。
# 任何把它留在 strict/warn 的測試，會讓後續寫 state（backlog/history）的測試在非 root
# 的 CI runner 上因 fchown(0,0) 失敗而 SecureWriteError（本機以 root 跑則 fchown 成功、
# 測不出來，純屬執行順序相依的假綠）。此 autouse fixture 在每個測試結束後把模組級全域
# 拉回 conftest 設定的 hermetic 基準 "off"，堵死洩漏，不影響測試內當下的 reload 斷言。
@pytest.fixture(autouse=True)
def _reset_require_chown_after_test():
    yield
    from studio import config as _config

    if _config.REQUIRE_CHOWN != "off":
        _config.REQUIRE_CHOWN = "off"


# --- 派工模式哨兵檔隔離：部署機殘留的 DISPATCH_AUTO 不得翻轉測試裡的派工行為 ------
# config.dispatch_auto() 讀 PROJECT_ROOT/DISPATCH_AUTO——在已切到 auto 模式的部署機上
# 跑測試（autodeploy sandbox pytest）時，殘留哨兵檔會讓所有「手動模式」既有測試默默
# 改道。每個測試一律指向 tmp（不存在＝手動基準）；auto 模式測試自行寫檔或 monkeypatch。
@pytest.fixture(autouse=True)
def _isolate_dispatch_auto_file(tmp_path):
    from studio import config as _config

    orig = _config.DISPATCH_AUTO_FILE
    _config.DISPATCH_AUTO_FILE = tmp_path / "DISPATCH_AUTO"
    yield
    _config.DISPATCH_AUTO_FILE = orig


# --- 防 provider_quota SWR 快取跨測試洩漏 ---------------------------------------
# provider_quota.snapshot() 有模組級 SWR 快取（_cache）＋背景刷新單飛執行緒。若不重置，
# 測試 A 佈置的假額度會被 60s TTL 快取住，測試 B 再呼叫 snapshot() 就拿到 A 的殘留快照
# （純執行順序相依的假綠/假紅）。每個測試開始前：先 join 掉上一個測試殘留的背景刷新
# （避免它在 monkeypatch 還原後才寫 _cache 污染本測試），再清空快取回到「無快取＝同步查」
# 的確定起點。
@pytest.fixture(autouse=True)
def _reset_provider_quota_cache():
    from studio import provider_quota as _pq

    _t = _pq._refresh_thread
    if _t is not None:
        _t.join(timeout=10)
    _pq._reset_cache()
    yield


# --- claude_usage 憑證隔離：測試不得誤讀部署機真實 ~/.claude 憑證 ------------------
# experts._model_for 末段的 scoped 改派會呼叫 claude_usage.fetch_rate_limits()（讀
# config.CLAUDE_CREDENTIALS_FILE）。部署機上該檔存在且可能 Fable 週限滿載，會讓「模型
# 選擇」類既有測試默默改道到備援模型（純環境相依的假紅）。每個測試預設把憑證指向不存在的
# tmp 檔（＝token_missing→error→不改派＝原模型），並清空 TTL/last-good 快取。需要真的
# 額度資料的測試自行 monkeypatch claude_usage.fetch_rate_limits（如 test_scoped_reroute），
# 或覆寫 CLAUDE_CREDENTIALS_FILE 指向自備 token（如 test_claude_usage，會蓋過本 fixture）。
@pytest.fixture(autouse=True)
def _isolate_claude_credentials(monkeypatch, tmp_path):
    from studio import claude_usage as _cu, config as _config

    monkeypatch.setattr(
        _config, "CLAUDE_CREDENTIALS_FILE", tmp_path / ".claude" / ".credentials.json"
    )
    _cu._cache.clear()
    _cu._last_good.clear()
    yield
    _cu._cache.clear()
    _cu._last_good.clear()


# 偵測 bwrap 實際是否可用（防止檔案存在但因權限無法使用造成測試紅燈）
def _check_bwrap_actually_works() -> bool:
    import subprocess

    from studio import config as _config

    try:
        # 必須鏡像真實 runner._bwrap_prefix 的 `--ro-bind / /`：bwrap 預設建立「完全空」
        # 的 mount namespace，未掛載 rootfs 時新 / 內找不到 `true` 可 exec，探測會在
        # 任何環境（含 bwrap 完全可用者）都回非 0 → 假性失敗 → 誤關沙箱讓 sandbox-test
        # job 因「測試被 skip」而紅。綁上 host 唯讀 rootfs 後，PATH 才找得到 true。
        res = subprocess.run(
            [_config.SANDBOX_BWRAP, "--ro-bind", "/", "/", "--unshare-pid", "--", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return res.returncode == 0
    except Exception:
        return False


if not _check_bwrap_actually_works():
    import warnings

    from studio import config as _config

    warnings.warn(
        f"bwrap sandbox probe failed for {_config.SANDBOX_BWRAP}; "
        "patching studio.config._sandbox_available=False for tests",
        RuntimeWarning,
        stacklevel=2,
    )
    _config._sandbox_available = lambda: False
