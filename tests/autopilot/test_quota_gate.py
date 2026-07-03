"""額度感知節奏（quota gate）：gate 純函式、主迴圈睡眠/續跑分支、心跳檔內容、config 開關。

gate 契約：provider_quota.gate(snap) -> (any_usable, earliest_reset_epoch)。
主迴圈契約：AUTOPILOT_QUOTA_GATE 開啟且全受限 → 寫 quota_sleep 心跳、睡
min(max(reset-now, 60), AUTOPILOT_QUOTA_MAX_SLEEP) 後 continue 重查；可用 → 取任務並寫
running 心跳；backlog 空 → 寫 idle 心跳。全部合成快照、不打外網。
"""

from __future__ import annotations

import asyncio as real_asyncio
import json
import time
import types

import pytest

from studio import autopilot, config, provider_quota as pq

# --- 合成快照 helper（與 tests/core/test_provider_quota_helpers.py 同構）----


def _snap(providers):
    return {"ok": True, "updated_at": time.time(), "providers": providers}


def _win(used, reset=None):
    """window 式 rate_limits（claude/codex/minimax）。"""
    return {"five_hour": {"used_percentage": used, "reset_at": reset}, "error": None}


# --- gate 純函式：正案 ------------------------------------------------------


def test_gate_usable_when_any_provider_has_quota():
    """只要有一個 provider ready、無 error、用量低於門檻 → any_usable=True。"""
    snap = _snap(
        [
            {"key": "claude", "ready": True, "rate_limits": _win(95, 2000)},
            {"key": "codex", "ready": True, "rate_limits": _win(20, 3000)},
        ]
    )
    usable, _reset = pq.gate(snap)
    assert usable is True


def test_gate_usable_without_usage_info():
    """ready、無 error、但拿不到用量資訊（max_used=None）→ 視為可用（與 constrained 對齊）。"""
    assert pq.gate(_snap([{"key": "claude", "ready": True, "rate_limits": {}}])) == (True, None)


def test_gate_threshold_reuses_constrained_threshold():
    """門檻 SSOT：恰達 CONSTRAINED_THRESHOLD 即受限，低 1 個百分點則可用。"""
    at = _snap([{"key": "c", "ready": True, "rate_limits": _win(pq.CONSTRAINED_THRESHOLD, 100)}])
    below = _snap([{"key": "c", "ready": True, "rate_limits": _win(pq.CONSTRAINED_THRESHOLD - 1)}])
    assert pq.gate(at)[0] is False
    assert pq.gate(below)[0] is True


# --- gate 純函式：反案 ------------------------------------------------------


def test_gate_all_constrained_returns_earliest_reset():
    """全受限 → any_usable=False，reset 取『就緒但額度耗盡』者的最早 reset_at。"""
    snap = _snap(
        [
            {"key": "claude", "ready": True, "rate_limits": _win(95, 5000)},
            {"key": "codex", "ready": True, "rate_limits": _win(91, 3000)},
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": True, "rate_limits": {"error": "unauthorized"}},
        ]
    )
    assert pq.gate(snap) == (False, 3000)


def test_gate_unready_or_error_only_no_reset():
    """只有未就緒/查詢異常的 provider → 不可用且無 reset 資訊（None，由呼叫端套下限）。"""
    snap = _snap(
        [
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": True, "rate_limits": {"error": "token_missing"}},
        ]
    )
    assert pq.gate(snap) == (False, None)


def test_gate_empty_snapshot_not_usable():
    assert pq.gate({"providers": []}) == (False, None)


# --- config：新開關進 config.py + reload() ---------------------------------


def test_config_quota_defaults():
    assert config.AUTOPILOT_QUOTA_GATE is True
    assert config.AUTOPILOT_QUOTA_MAX_SLEEP == 1800


def test_config_reload_reads_quota_env(monkeypatch):
    monkeypatch.setenv("TI_AUTOPILOT_QUOTA_GATE", "0")
    monkeypatch.setenv("TI_AUTOPILOT_QUOTA_MAX_SLEEP", "900")
    try:
        config.reload()
        assert config.AUTOPILOT_QUOTA_GATE is False
        assert config.AUTOPILOT_QUOTA_MAX_SLEEP == 900
    finally:
        monkeypatch.undo()
        config.reload()


# --- 主迴圈：monkeypatch snapshot，斷言 sleep/continue 分支與心跳檔 ---------


class _Stop(BaseException):
    """跳出 main() 無限迴圈用；繼承 BaseException 以免被 except Exception 吃掉。"""


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", True)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_MAX_SLEEP", 1800)
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "autopilot_paused", lambda: False)
    return tmp_path / "state"


def _read_status(state_dir):
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


def _stub_asyncio(monkeypatch, sleeps):
    """把 autopilot 模組內的 asyncio 換成 stub：sleep 記錄秒數後丟 _Stop 跳出迴圈。

    只動 autopilot 的名字空間（to_thread 委派給真 asyncio），不污染全域 asyncio。
    """

    async def fake_sleep(s):
        sleeps.append(s)
        raise _Stop

    monkeypatch.setattr(
        autopilot,
        "asyncio",
        types.SimpleNamespace(to_thread=real_asyncio.to_thread, sleep=fake_sleep),
    )


async def test_main_loop_quota_sleep_until_reset_and_heartbeat(monkeypatch, state_dir):
    """全受限 → 不取任務、睡 ≈(reset-now) 秒、心跳寫 quota_sleep（含 sleep_until/quota）。"""
    now = time.time()
    snap = _snap(
        [
            {"key": "claude", "ready": True, "rate_limits": _win(95, now + 600)},
            {"key": "codex", "ready": False, "rate_limits": None},
        ]
    )
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    monkeypatch.setattr(
        autopilot.backlog, "next_pending", lambda: pytest.fail("全受限時不得取任務")
    )
    sleeps: list[float] = []
    _stub_asyncio(monkeypatch, sleeps)

    with pytest.raises(_Stop):
        await autopilot.main()

    assert len(sleeps) == 1 and 500 <= sleeps[0] <= 600  # ≈ reset-now，落在 [60, MAX_SLEEP]
    status = _read_status(state_dir)
    assert status["state"] == "quota_sleep"
    assert status["task_id"] is None
    assert status["sleep_until"] == pytest.approx(now + 600, abs=30)
    assert status["updated_at"] == pytest.approx(time.time(), abs=30)
    assert status["quota"] == {"claude": 95, "codex": None}


@pytest.mark.parametrize(
    "reset_offset,expected",
    [
        (None, 60.0),  # 無 reset 資訊 → 下限 60 秒
        (-120, 60.0),  # reset 已過（負等待）→ 下限 60 秒
        (10**6, 1800.0),  # reset 太遠 → 上限 AUTOPILOT_QUOTA_MAX_SLEEP
    ],
)
async def test_main_loop_quota_sleep_clamped(monkeypatch, state_dir, reset_offset, expected):
    """睡眠秒數必為 min(max(reset-now, 60), MAX_SLEEP)。"""
    reset = None if reset_offset is None else time.time() + reset_offset
    snap = _snap([{"key": "claude", "ready": True, "rate_limits": _win(99, reset)}])
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    sleeps: list[float] = []
    _stub_asyncio(monkeypatch, sleeps)

    with pytest.raises(_Stop):
        await autopilot.main()

    assert sleeps == [pytest.approx(expected, abs=1)]


async def test_main_loop_usable_takes_task_and_writes_running(monkeypatch, state_dir):
    """有 provider 可用 → 正常取任務跑，心跳寫 running（含 task_id 與 quota 摘要）。"""
    snap = _snap([{"key": "claude", "ready": True, "rate_limits": _win(10, time.time() + 60)}])
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: {"id": 42, "title": "任務"})
    ran: list[dict] = []

    async def fake_run(task):
        ran.append(task)
        raise _Stop

    monkeypatch.setattr(autopilot, "run_one_task", fake_run)
    with pytest.raises(_Stop):
        await autopilot.main()

    assert [t["id"] for t in ran] == [42]
    status = _read_status(state_dir)
    assert status["state"] == "running"
    assert status["task_id"] == 42
    assert status["sleep_until"] is None
    assert status["quota"] == {"claude": 10}


async def test_main_loop_idle_heartbeat_when_backlog_empty(monkeypatch, state_dir):
    """backlog 空 → 進自我評估前心跳寫 idle。"""
    snap = _snap([{"key": "claude", "ready": True, "rate_limits": _win(10)}])
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: None)

    async def fake_clone():
        return "/nonexistent"

    async def fake_eval(_clone):
        raise _Stop  # idle 心跳應已寫入

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_clone)
    monkeypatch.setattr(autopilot, "_evaluate_self", fake_eval)
    with pytest.raises(_Stop):
        await autopilot.main()

    status = _read_status(state_dir)
    assert status["state"] == "idle" and status["task_id"] is None


async def test_main_loop_gate_disabled_skips_snapshot(monkeypatch, state_dir):
    """AUTOPILOT_QUOTA_GATE=0 → 不查快照（維持舊行為），仍寫 running 心跳（quota 空）。"""
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(
        autopilot.provider_quota, "snapshot", lambda: pytest.fail("gate 關閉時不得查快照")
    )
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: {"id": 7, "title": "t"})

    async def fake_run(_task):
        raise _Stop

    monkeypatch.setattr(autopilot, "run_one_task", fake_run)
    with pytest.raises(_Stop):
        await autopilot.main()

    status = _read_status(state_dir)
    assert status["state"] == "running" and status["quota"] == {}


def test_write_status_failure_does_not_raise(monkeypatch, state_dir):
    """心跳只是輔助觀測：secure_write_root 炸掉不得往外拋（不影響主迴圈）。"""

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(autopilot, "secure_write_root", boom)
    autopilot._write_status("idle")  # 不應 raise
