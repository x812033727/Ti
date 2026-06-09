"""P1：deploy 互斥鎖——確保並行部署只有一個會真的跑，另一個被擋（非阻塞 flock）。"""

from __future__ import annotations

import fcntl

import pytest

from studio import deploy


def test_deploy_lock_is_exclusive_and_nonblocking(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy.config, "AUTOPILOT_STATE_DIR", tmp_path)
    with deploy._deploy_lock() as first:
        assert first is True
        with deploy._deploy_lock() as second:
            assert second is False  # 同檔不同 fd、非阻塞 → 第二個拿不到
    # 釋放後可再取得
    with deploy._deploy_lock() as again:
        assert again is True


@pytest.mark.asyncio
async def test_redeploy_skips_when_another_deploy_holds_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy.config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(deploy.config, "AUTOPILOT_DRYRUN", False)
    # 外部先持鎖，模擬另一條部署路徑正在跑
    held = (tmp_path / "deploy.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        ok, msg = await deploy.redeploy()
        assert ok is False
        assert "另一個部署進行中" in msg  # 早退、不碰 git/pip/systemctl
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
