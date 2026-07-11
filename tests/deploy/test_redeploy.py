"""測試重新佈署重啟 redeploy（純邏輯 + endpoint，以 mock 取代真的 pull/execv）。"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from studio import config, deploy, redeploy, runner


@pytest.fixture
def client():
    from studio.server import app

    # 寫入端點門禁停用時 fail-safe 限本機（require_admin）：以 loopback peer 連入測端點合約。
    return TestClient(app, client=("127.0.0.1", 12345))


@pytest.fixture(autouse=True)
def _no_real_restart(monkeypatch, tmp_path):
    """確保測試永遠不會真的 exec 掉行程；並讓 exec 前的 import smoke 預設通過
    （個別測試可再覆寫成失敗）。"""
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: None)

    async def _smoke_ok():
        return runner.RunOutput("import smoke", 0, "", False)

    monkeypatch.setattr(redeploy, "import_smoke", _smoke_ok)


def test_redact_masks_token(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_secret")
    out = redeploy._redact("error using ghp_secret here")
    assert "ghp_secret" not in out
    assert "***" in out


@pytest.mark.asyncio
async def test_redeploy_busy_when_lock_not_acquired(monkeypatch):
    @contextmanager
    def fake_busy_lock():
        yield False

    calls = {"pull": 0, "restart": 0}

    async def fake_pull():
        calls["pull"] += 1
        return runner.RunOutput("git pull", 0, "ok", False)

    monkeypatch.setattr(deploy, "_deploy_lock", fake_busy_lock)
    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    monkeypatch.setattr(
        redeploy, "schedule_restart", lambda *a, **k: calls.__setitem__("restart", 1)
    )

    res = await redeploy.redeploy()
    assert not res["ok"] and not res["pulled"] and not res["restarting"]
    assert "部署進行中" in res["detail"]
    assert calls == {"pull": 0, "restart": 0}


@pytest.mark.asyncio
async def test_redeploy_pull_success_schedules_restart(monkeypatch):
    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    calls = {"n": 0}
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: calls.__setitem__("n", 1))

    res = await redeploy.redeploy()
    assert res["ok"] and res["pulled"] and res["restarting"]
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_redeploy_pull_failure_no_restart(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_secret")

    async def fake_pull():
        return runner.RunOutput("git pull", 1, "fatal: auth ghp_secret failed", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    calls = {"n": 0}
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: calls.__setitem__("n", 1))

    res = await redeploy.redeploy()
    assert not res["ok"] and not res["pulled"] and not res["restarting"]
    assert calls["n"] == 0
    assert "ghp_secret" not in res["detail"]  # token 已遮蔽


@pytest.mark.asyncio
async def test_redeploy_import_smoke_failure_no_restart(monkeypatch):
    """新版 import 檢查失敗時，必須取消重啟、不丟例外、不外洩 token，服務維持舊版。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_secret")

    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Updating to broken main", False)

    async def fake_smoke_fail():
        return runner.RunOutput("import smoke", 1, "SyntaxError in studio/foo.py ghp_secret", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    monkeypatch.setattr(redeploy, "import_smoke", fake_smoke_fail)
    calls = {"n": 0}
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: calls.__setitem__("n", 1))

    res = await redeploy.redeploy()
    assert not res["ok"] and not res["restarting"]
    assert calls["n"] == 0  # 沒有排程重啟
    assert "ghp_secret" not in res["detail"]  # token 已遮蔽


@pytest.mark.asyncio
async def test_redeploy_no_restart_flag(monkeypatch):
    async def fake_pull():
        return runner.RunOutput("git pull", 0, "ok", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    res = await redeploy.redeploy(restart=False)
    assert res["ok"] and res["pulled"] and not res["restarting"]


def test_redeploy_endpoint(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")

    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    res = client.post("/api/redeploy")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] and body["restarting"]


def test_redeploy_endpoint_blocked_when_gated(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    assert client.post("/api/redeploy").status_code == 401
