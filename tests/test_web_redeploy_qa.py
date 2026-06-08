"""QA 加固測試：任務 #4 —— 前端「合併並重啟」入口與結果顯示。

前端為純 JS（無瀏覽器測試框架），故以兩個層面驗證：
A. 前端結構：對 web/app.js 做靜態斷言，確認 UI 接線存在且沿用既有 publish CTA 樣式。
B. 後端合約：前端依賴的 API（/api/publish/config 的 merge、/api/redeploy）回傳符合預期。
JS 語法另以 `node --check` 於命令列驗證（已通過）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, redeploy, runner

APP_JS = config.PROJECT_ROOT / "web" / "app.js"
STYLES = config.PROJECT_ROOT / "web" / "styles.css"


@pytest.fixture
def app_js():
    return APP_JS.read_text(encoding="utf-8")


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
    # 合併成功後出現「重新佈署並重啟」入口，打 POST /api/redeploy
    assert "addRedeployButton" in app_js
    assert "重新佈署並重啟" in app_js
    assert '"/api/redeploy"' in app_js
    assert "renderRedeploy" in app_js


def test_redeploy_shown_only_after_merge(app_js):
    # merged 成功才顯示「已合併」並掛上 redeploy 入口
    assert "p.merged" in app_js
    assert "已合併" in app_js
    assert "addRedeployButton()" in app_js


def test_reuses_publish_cta_styles(app_js):
    # 沿用現有 publish CTA 樣式（publish-cta / publish）
    assert app_js.count('"publish-cta"') >= 2  # publish 與 redeploy 兩顆按鈕都用
    assert STYLES.read_text(encoding="utf-8").find(".publish-cta") != -1


def test_redeploy_request_failure_handled(app_js):
    # redeploy 請求失敗（服務可能正在重啟）有 catch 並顯示可讀訊息
    assert "重新佈署請求失敗" in app_js


# --- B. 後端合約（前端所依賴）---------------------------------------
@pytest.fixture
def client():
    from studio.server import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_real_restart(monkeypatch):
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: None)


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
    r = client.get("/app.js")
    assert r.status_code == 200
    assert "addRedeployButton" in r.text
