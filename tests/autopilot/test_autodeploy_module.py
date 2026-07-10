"""studio.autodeploy 納管版（完成率第三輪修法二A 收尾）。

覆蓋：無 drift→略過並清 deferred 檔；有 drift＋討論中→延後並累計 deferred 觀測檔
（同 remote 累計、換 remote 重計、壞檔重計）；有 drift＋idle→redeploy，成功清 deferred；
fetch 失敗→rc=1。全程 stub deploy/history，零真實 git/部署。
"""

from __future__ import annotations

import json

import pytest

from studio import autodeploy, config, deploy


class DeployStub:
    def __init__(self, *, disk="aaa111", origin="bbb222", fetch_rc=0, redeploy_ok=True):
        self.disk = disk
        self.origin = origin
        self.fetch_rc = fetch_rc
        self.redeploy_ok = redeploy_ok
        self.redeploy_calls = 0

    async def run(self, cmd, cwd=None, timeout=600):
        joined = " ".join(cmd)
        if "fetch" in joined:
            return (self.fetch_rc, "" if self.fetch_rc == 0 else "network down")
        if "rev-parse" in joined:
            return (0, self.origin + "\n")
        return (0, "")

    async def current_head(self, repo_dir):
        return self.disk

    async def redeploy(self):
        self.redeploy_calls += 1
        return (self.redeploy_ok, "ok" if self.redeploy_ok else "健康檢查失敗→回滾成功")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    return tmp_path


def _install(monkeypatch, stub: DeployStub, *, busy=False):
    monkeypatch.setattr(deploy, "_run", stub.run)
    monkeypatch.setattr(deploy, "current_head", stub.current_head)
    monkeypatch.setattr(deploy, "redeploy", stub.redeploy)
    import studio.history as history_mod

    monkeypatch.setattr(
        history_mod, "busy_sessions", lambda *_a, **_k: [{"session_id": "m1"}] if busy else []
    )


def _deferred(tmp_path):
    p = tmp_path / "ap" / "autodeploy-deferred.json"
    return json.loads(p.read_text()) if p.exists() else None


@pytest.mark.asyncio
async def test_no_drift_skips_and_clears_deferred(state, monkeypatch, tmp_path):
    (tmp_path / "ap" / "autodeploy-deferred.json").write_text('{"deferrals": 5, "remote": "x"}')
    stub = DeployStub(disk="same", origin="same")
    _install(monkeypatch, stub)

    assert await autodeploy.run_once() == 0
    assert stub.redeploy_calls == 0
    assert _deferred(tmp_path) is None, "無 drift 應清掉 deferred 觀測檔"


@pytest.mark.asyncio
async def test_busy_defers_and_accumulates_observation(state, monkeypatch, tmp_path):
    stub = DeployStub()
    _install(monkeypatch, stub, busy=True)

    assert await autodeploy.run_once() == 0
    assert await autodeploy.run_once() == 0
    d = _deferred(tmp_path)
    assert stub.redeploy_calls == 0
    assert d["deferrals"] == 2 and d["remote"] == "bbb222"
    assert d["first_deferred_at"] > 0


@pytest.mark.asyncio
async def test_new_remote_resets_deferred_counter(state, monkeypatch, tmp_path):
    stub = DeployStub(origin="ccc333")
    _install(monkeypatch, stub, busy=True)
    (tmp_path / "ap" / "autodeploy-deferred.json").write_text(
        '{"first_deferred_at": 1.0, "deferrals": 9, "remote": "bbb222"}'
    )

    await autodeploy.run_once()
    d = _deferred(tmp_path)
    assert d["deferrals"] == 1, "目標 commit 換了應重計"
    assert d["remote"] == "ccc333"


@pytest.mark.asyncio
async def test_idle_redeploys_and_clears_deferred(state, monkeypatch, tmp_path):
    (tmp_path / "ap" / "autodeploy-deferred.json").write_text('{"deferrals": 3, "remote": "b"}')
    stub = DeployStub()
    _install(monkeypatch, stub)

    assert await autodeploy.run_once() == 0
    assert stub.redeploy_calls == 1
    assert _deferred(tmp_path) is None


@pytest.mark.asyncio
async def test_fetch_failure_returns_nonzero(state, monkeypatch):
    stub = DeployStub(fetch_rc=1)
    _install(monkeypatch, stub)
    assert await autodeploy.run_once() == 1
