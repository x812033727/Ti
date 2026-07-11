"""QA 加固測試：任務 #4 —— 前端「重新部署並重啟」入口與結果顯示。

重新部署為設定面板常駐入口（非合併後才出現的串內按鈕）。前端為純 JS
（無瀏覽器測試框架），故以兩個層面驗證：
A. 前端結構：對 web/app.js、web/index.html、web/styles.css 做靜態斷言，
   確認設定面板的重新部署 UI 接線、確認框與樣式存在。
B. 後端合約：前端依賴的 API（/api/publish/config 的 merge、/api/redeploy）回傳符合預期。
JS 語法另以 `node --check` 於命令列驗證（已通過）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, redeploy, runner

APP_JS = config.PROJECT_ROOT / "web" / "app.js"
STYLES = config.PROJECT_ROOT / "web" / "styles.css"
INDEX = config.PROJECT_ROOT / "web" / "index.html"


def _frontend_js_source() -> str:
    """前端 JS 聚合原始碼：入口 app.js + web/js/ 全部模組（ES module 拆分後，
    字串斷言對「整個前端」成立即可，不綁死單一檔案）。"""
    parts = [APP_JS.read_text(encoding="utf-8")]
    for p in sorted((config.PROJECT_ROOT / "web" / "js").rglob("*.js")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.fixture
def app_js():
    return _frontend_js_source()


# --- A. 前端結構斷言 ------------------------------------------------
def test_loads_merge_flag_from_config(app_js):
    # 從 /api/publish/config 讀取 merge 旗標，決定按鈕文案
    assert "/api/publish/config" in app_js
    assert "mergeEnabled" in app_js
    assert "cfg.merge" in app_js


def test_merge_cta_text(app_js):
    # merge 開啟時 CTA 明示「發佈並合併」
    assert "發佈並合併到 GitHub" in app_js
    assert "發佈成果到 GitHub" in app_js  # 關閉時維持原文案（向後相容）


def test_redeploy_button_wiring(app_js):
    # 設定面板常駐「重新部署並重啟」入口，按下打 POST /api/redeploy
    index = INDEX.read_text(encoding="utf-8")
    assert "redeployBtn" in index
    assert "重新部署並重啟" in index
    assert "redeployNow" in app_js
    assert '"/api/redeploy"' in app_js
    assert '$("#redeployBtn").onclick = redeployNow' in app_js


def test_redeploy_entry_in_settings_panel(app_js):
    # 重新部署入口位於設定面板（settings-redeploy 區塊），按下前先確認
    index = INDEX.read_text(encoding="utf-8")
    assert "settings-redeploy" in index
    assert "redeployStatus" in index
    assert "確定重新部署" in app_js  # redeployNow 內先 confirm 再執行


def test_redeploy_block_has_styles(app_js):
    # 重新部署區塊有對應樣式（settings-redeploy / redeploy-status）
    # styles.css 已拆為 @import 聚合檔：斷言對「聚合後的全部 CSS」成立即可
    styles = STYLES.read_text(encoding="utf-8")
    for p in sorted((config.PROJECT_ROOT / "web" / "css").glob("*.css")):
        styles += p.read_text(encoding="utf-8")
    assert ".settings-redeploy" in styles
    assert ".redeploy-status" in styles


def test_redeploy_request_failure_handled(app_js):
    # redeploy 請求失敗（服務可能正在重啟）有 catch 並顯示可讀訊息
    assert "服務可能正在重啟" in app_js


# --- B. 後端合約（前端所依賴）---------------------------------------
@pytest.fixture
def client():
    from studio.server import app

    # 寫入端點門禁停用時 fail-safe 限本機（require_admin）：以 loopback peer 連入測後端合約。
    return TestClient(app, client=("127.0.0.1", 12345))


@pytest.fixture(autouse=True)
def _no_real_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: None)

    async def _smoke_ok():
        return runner.RunOutput("import smoke", 0, "", False)

    monkeypatch.setattr(redeploy, "import_smoke", _smoke_ok)


def test_publish_config_exposes_merge(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "PUBLISH_MERGE", True)
    body = client.get("/api/publish/config").json()
    assert "merge" in body and body["merge"] is True
    assert "configured" in body  # 前端 publishConfigured 依賴此欄位


def test_redeploy_endpoint_contract(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")

    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    body = client.post("/api/redeploy").json()
    # 前端 renderRedeploy 依賴 ok / detail
    assert "ok" in body and "detail" in body
    assert body["ok"] is True


def test_static_app_js_served(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "redeployNow" in r.text
