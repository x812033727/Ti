"""`deploy.drift_stats` 部署漂移觀測（完成率第三輪修法二A 的可觀測面）。

覆蓋：欄位正確、TTL 快取（第二次呼叫不再 fork git）、git 失敗容錯回空欄、
deferred 觀測檔（缺檔 None／壞檔 None／合法 dict 透傳）。全程 stub `_run`，零真實 git。
"""

from __future__ import annotations

import json

import pytest

from studio import config, deploy


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_DIR", tmp_path / "repo")
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    # 清 TTL 快取（模組級）
    monkeypatch.setattr(deploy, "_drift_cache", {"ts": 0.0, "data": None})
    return tmp_path


def _stub_git(monkeypatch, *, head="a" * 40, origin="b" * 40, behind="3", fail=False):
    calls: list[str] = []

    async def _run(cmd, cwd=None, timeout=600, **kwargs):
        joined = " ".join(cmd)
        calls.append(joined)
        if fail:
            return (128, "fatal: not a git repository")
        if "rev-parse HEAD" in joined:
            return (0, head + "\n")
        if "rev-parse origin/main" in joined:
            return (0, origin + "\n")
        if "rev-list --count" in joined:
            return (0, behind + "\n")
        return (0, "")

    monkeypatch.setattr(deploy, "_run", _run)
    return calls


@pytest.mark.asyncio
async def test_fields_and_ttl_cache(state, monkeypatch):
    calls = _stub_git(monkeypatch)
    out = await deploy.drift_stats()

    assert out["disk_head"] == "a" * 12 and out["origin_head"] == "b" * 12
    assert out["behind"] == 3
    assert out["deferred"] is None, "無觀測檔＝None"

    n = len(calls)
    out2 = await deploy.drift_stats()
    assert out2 == out
    assert len(calls) == n, "TTL 內第二次呼叫不得再 fork git"


@pytest.mark.asyncio
async def test_git_failure_returns_empty_fields(state, monkeypatch):
    _stub_git(monkeypatch, fail=True)
    out = await deploy.drift_stats()

    assert out["disk_head"] == "" and out["origin_head"] == ""
    assert out["behind"] is None


@pytest.mark.asyncio
async def test_deferred_file_passthrough_and_corruption(state, monkeypatch, tmp_path):
    deferred = {"first_deferred_at": 1.0, "deferrals": 7}
    (tmp_path / "ap" / "autodeploy-deferred.json").write_text(json.dumps(deferred))
    _stub_git(monkeypatch)
    out = await deploy.drift_stats()
    assert out["deferred"] == deferred

    # 壞檔 → None（觀測面不得拋）
    monkeypatch.setattr(deploy, "_drift_cache", {"ts": 0.0, "data": None})
    (tmp_path / "ap" / "autodeploy-deferred.json").write_text("{broken json")
    out = await deploy.drift_stats()
    assert out["deferred"] is None
