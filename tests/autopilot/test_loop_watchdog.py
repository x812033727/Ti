"""主迴圈心跳看門狗(穩定強化 β)+ provider_quota 探測逾時補強。

盲區:stall/hard 看門狗只包 session.run,「任務之間」(quota snapshot/reconciler/邊界
部署/triage)任一步無聲卡死=整台停擺且無 log。

守護不變量:
- tick 停滯(非暫停、非任務中)→ log.error 一次;恢復後可再告警;任務執行中不告警;
  暫停中不告警;旋鈕 0=關。
- provider_quota._fetch:單一探測卡死(future 永不完成)→ 該家回 None,其他家照常,
  _fetch 在探測上限內返回(不被 shutdown(wait=True) 拖死)。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from studio import autopilot, config


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_LOOP_STALL_S", 100)
    monkeypatch.setattr(autopilot, "_task_running", False)
    monkeypatch.setattr(autopilot, "_loop_tick_at", time.time())


async def _run_monitor_once(monkeypatch):
    """讓 monitor 跑一輪檢查後取消(fast sleep)。"""
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await autopilot._loop_monitor()


@pytest.mark.asyncio
async def test_stalled_tick_alerts_once(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(autopilot, "_loop_tick_at", time.time() - 500)
    with caplog.at_level(logging.ERROR, logger="ti.autopilot"):
        await _run_monitor_once(monkeypatch)
    assert sum("主迴圈心跳停滯" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup",
    ["task_running", "paused", "knob_off", "fresh_tick"],
)
async def test_no_alert_in_benign_states(monkeypatch, caplog, setup):
    import logging

    monkeypatch.setattr(autopilot, "_loop_tick_at", time.time() - 500)
    if setup == "task_running":
        monkeypatch.setattr(autopilot, "_task_running", True)
    elif setup == "paused":
        monkeypatch.setattr(config, "autopilot_paused", lambda: True)
    elif setup == "knob_off":
        monkeypatch.setattr(config, "AUTOPILOT_LOOP_STALL_S", 0)
    elif setup == "fresh_tick":
        monkeypatch.setattr(autopilot, "_loop_tick_at", time.time())
    with caplog.at_level(logging.ERROR, logger="ti.autopilot"):
        await _run_monitor_once(monkeypatch)
    assert not any("主迴圈心跳停滯" in r.getMessage() for r in caplog.records), setup


def test_loop_tick_advances():
    before = autopilot._loop_tick_at
    time.sleep(0.01)
    autopilot._loop_tick()
    assert autopilot._loop_tick_at > before


# --- provider_quota 探測逾時 -----------------------------------------------------


def test_fetch_survives_wedged_probe(monkeypatch):
    """單一 provider 探測卡死:該家 None、其他家照常,_fetch 有界返回。"""
    import threading

    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "_PROBE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(provider_quota.config, "claude_cli_logged_in", lambda: True)
    monkeypatch.setattr(provider_quota.config, "has_api_key", lambda: False)
    monkeypatch.setattr(provider_quota.config, "codex_cli_available", lambda: False)
    monkeypatch.setattr(provider_quota.config, "MINIMAX_API_KEY", "")
    monkeypatch.setattr(provider_quota.claude_accounts, "sync_active_label", lambda: None)
    monkeypatch.setattr(provider_quota.claude_accounts, "list_accounts", lambda: [])

    release = threading.Event()

    def wedged(*a, **k):
        release.wait(5)  # 模擬卡死(遠超 _PROBE_TIMEOUT_S)
        return None

    monkeypatch.setattr(provider_quota.claude_usage, "fetch_rate_limits", wedged)
    monkeypatch.setattr(provider_quota, "_antigravity_status", lambda: {"ok": True})

    t0 = time.time()
    out = provider_quota._fetch()
    elapsed = time.time() - t0
    release.set()

    assert elapsed < 3, f"卡死探測不得拖垮 _fetch(耗時 {elapsed:.1f}s)"
    assert isinstance(out, dict), "其他 provider 照常回傳"
