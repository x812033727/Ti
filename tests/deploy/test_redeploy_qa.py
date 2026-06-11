"""QA 加固測試：任務 #3 —— 重新佈署重啟機制。

涵蓋既有 test_redeploy.py 未明確驗證的角度：
- pull_main 在 PROJECT_ROOT 執行 `git pull --ff-only`。
- _do_restart 以 sys.executable + sys.argv re-exec（不真的 exec，攔截 os.execv）。
- schedule_restart 透過 event loop call_later 排程 _do_restart。
- redeploy() 回傳 dict 形狀正確、token 遮蔽。
- /api/redeploy 僅接受 POST（GET→405）、門禁啟用時需登入（401）。
- /api/health 在重啟後可正常回應（以 TestClient 代表新版行程）。
"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from studio import config, redeploy, runner

# 在 autouse fixture 之前（模組載入時）保存原始函式，供直接測試。
_ORIG_SCHEDULE_RESTART = redeploy.schedule_restart


@pytest.fixture
def client():
    from studio.server import app

    # 寫入端點門禁停用時 fail-safe 限本機（require_admin）：以 loopback peer 連入。
    return TestClient(app, client=("127.0.0.1", 12345))


@pytest.fixture(autouse=True)
def _no_real_restart(monkeypatch):
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: None)


# --- pull_main ------------------------------------------------------
@pytest.mark.asyncio
async def test_pull_main_uses_project_root_ff_only(monkeypatch):
    captured = {}

    async def fake_run(cwd, argv, timeout=None, sandbox=None, label=None):
        captured["cwd"] = cwd
        captured["argv"] = argv
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    monkeypatch.setattr(runner, "run_command_exec", fake_run)
    out = await redeploy.pull_main()
    assert out.ok
    assert captured["cwd"] == config.PROJECT_ROOT
    # argv 式、fast-forward only，避免意外 merge commit
    assert captured["argv"] == ["git", "pull", "--ff-only"]


# --- _do_restart / schedule_restart ---------------------------------
def test_do_restart_reexecs_with_argv(monkeypatch):
    calls = {}

    def fake_execv(path, argv):
        calls["path"] = path
        calls["argv"] = argv

    monkeypatch.setattr(redeploy.os, "execv", fake_execv)
    redeploy._do_restart()
    assert calls["path"] == sys.executable
    # 以原啟動參數重啟：argv[0] 為 python，其後沿用 sys.argv
    assert calls["argv"][0] == sys.executable
    assert calls["argv"][1:] == sys.argv


@pytest.mark.asyncio
async def test_schedule_restart_uses_call_later(monkeypatch):
    import asyncio

    scheduled = {}

    class FakeLoop:
        def call_later(self, delay, fn):
            scheduled["delay"] = delay
            scheduled["fn"] = fn

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())
    # autouse fixture 已把 redeploy.schedule_restart 換掉，用模組載入時保存的原函式來測
    _ORIG_SCHEDULE_RESTART(delay=1.5)
    assert scheduled["delay"] == 1.5
    assert scheduled["fn"] is redeploy._do_restart


# --- redeploy() dict 形狀 + 遮蔽 ------------------------------------
@pytest.mark.asyncio
async def test_redeploy_dict_shape(monkeypatch):
    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    res = await redeploy.redeploy()
    assert set(res) == {"ok", "pulled", "restarting", "detail"}
    assert res["ok"] and res["pulled"] and res["restarting"]


@pytest.mark.asyncio
async def test_redeploy_failure_redacts_token(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_topsecret")

    async def fake_pull():
        return runner.RunOutput("git pull", 1, "auth failed ghp_topsecret", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    res = await redeploy.redeploy()
    assert not res["ok"] and not res["restarting"]
    assert "ghp_topsecret" not in res["detail"]


# --- endpoint ------------------------------------------------------
def test_redeploy_get_not_allowed(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    assert client.get("/api/redeploy").status_code == 405  # 僅 POST


def test_redeploy_post_ok(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")

    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    body = client.post("/api/redeploy").json()
    assert body["ok"] and body["restarting"]


def test_redeploy_gated_requires_auth(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    assert client.post("/api/redeploy").status_code == 401


def test_health_ok_represents_new_code(client):
    # 重啟後行程即以此程式碼回應健康檢查
    body = client.get("/api/health").json()
    assert body["ok"] is True
