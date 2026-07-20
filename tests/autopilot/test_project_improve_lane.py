"""專案自主排水旁路(軌 H):跨行程改良鎖互斥/旁路閘門/每日 throttle/逐專案排水。"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, backlog, config, improver as impmod, projects


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


async def _noop(_ev):
    return None


# --- H1:ProjectImprover.run 跨行程鎖 ---------------------------------------


@pytest.mark.asyncio
async def test_improve_lock_mutual_exclusion_and_release(monkeypatch):
    meta = projects.create("鎖測試", vision="")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(self, max_cycles=None):
        started.set()
        await release.wait()
        return {"cycles": 1, "done": 1, "failed": 0, "stopped": False}

    monkeypatch.setattr(impmod.ProjectImprover, "_run_unlocked", slow)
    a = impmod.ProjectImprover(meta, _noop)
    b = impmod.ProjectImprover(meta, _noop)
    ta = asyncio.create_task(a.run())
    await asyncio.wait_for(started.wait(), 5)
    rb = await b.run()
    assert rb["stopped"] is True and rb["cycles"] == 0, "持鎖期間第二場直接收場"
    release.set()
    ra = await asyncio.wait_for(ta, 5)
    assert ra["done"] == 1
    rc = await b.run()
    assert rc["done"] == 1, "釋放後可再進場"


# --- H2:旁路閘門與排水 -------------------------------------------------------


async def _run_lane_once(monkeypatch):
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await autopilot._project_improve_lane()


class _FakeImprover:
    calls: list = []

    def __init__(self, project, broadcast, intervention_queue=None):
        self.project = project

    async def run(self, max_cycles=None):
        _FakeImprover.calls.append((self.project.get("name"), max_cycles))
        return {"cycles": 1, "done": 1, "failed": 0, "stopped": False}


@pytest.fixture
def lane_on(monkeypatch):
    _FakeImprover.calls = []
    monkeypatch.setattr(config, "PROJECT_IMPROVE_AUTO", True)
    monkeypatch.setattr(config, "PROJECT_IMPROVE_CYCLES", 1)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)
    monkeypatch.setattr(autopilot, "_project_improve_day", None)
    monkeypatch.setattr(impmod, "ProjectImprover", _FakeImprover)


@pytest.mark.asyncio
async def test_lane_drains_intent_projects_only(monkeypatch, lane_on):
    m1 = projects.create("有意圖", vision="v")
    projects.set_intent(m1["id"], "北極星")
    projects.create("無意圖", vision="v")
    await _run_lane_once(monkeypatch)
    assert _FakeImprover.calls == [("有意圖", 1)], "只排水有 intent 的專案"


@pytest.mark.asyncio
async def test_lane_daily_throttle(monkeypatch, lane_on):
    m1 = projects.create("有意圖", vision="v")
    projects.set_intent(m1["id"], "北極星")
    await _run_lane_once(monkeypatch)
    assert len(_FakeImprover.calls) == 1, "同日第二輪不重跑(fast_sleep 給了兩次機會)"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["knob_off", "paused"])
async def test_lane_respects_gates(monkeypatch, lane_on, gate):
    m1 = projects.create("有意圖", vision="v")
    projects.set_intent(m1["id"], "北極星")
    if gate == "knob_off":
        monkeypatch.setattr(config, "PROJECT_IMPROVE_AUTO", False)
    else:
        monkeypatch.setattr(config, "autopilot_paused", lambda: True)
    await _run_lane_once(monkeypatch)
    assert _FakeImprover.calls == []


@pytest.mark.asyncio
async def test_lane_project_failure_isolated(monkeypatch, lane_on):
    m1 = projects.create("會炸", vision="v")
    projects.set_intent(m1["id"], "x")
    m2 = projects.create("正常", vision="v")
    projects.set_intent(m2["id"], "y")

    async def boom_or_ok(self, max_cycles=None):
        if self.project.get("name") == "會炸":
            raise RuntimeError("session down")
        _FakeImprover.calls.append((self.project.get("name"), max_cycles))
        return {"cycles": 1, "done": 1, "failed": 0, "stopped": False}

    monkeypatch.setattr(_FakeImprover, "run", boom_or_ok)
    await _run_lane_once(monkeypatch)
    assert ("正常", 1) in _FakeImprover.calls, "單一專案炸不影響其他專案"


# --- H 補丁:execv 守門/每日 throttle 落檔/專案 stale 回收 --------------------


def test_improve_lane_blocking_reload(monkeypatch):
    import time as _t

    monkeypatch.setattr(autopilot, "_improve_lane_busy", None)
    assert autopilot._improve_lane_blocking_reload() is False
    monkeypatch.setattr(autopilot, "_improve_lane_busy", {"pid": "x", "started_at": _t.time()})
    assert autopilot._improve_lane_blocking_reload() is True, "進行中=擋 execv"
    monkeypatch.setattr(
        autopilot,
        "_improve_lane_busy",
        {"pid": "x", "started_at": _t.time() - autopilot._IMPROVE_BUSY_MAX_AGE - 1},
    )
    assert autopilot._improve_lane_blocking_reload() is False, "超齡懸掛不得永久釘死重載"


@pytest.mark.asyncio
async def test_lane_day_marker_survives_restart(monkeypatch, lane_on):
    m1 = projects.create("有意圖", vision="v")
    projects.set_intent(m1["id"], "北極星")
    await _run_lane_once(monkeypatch)
    assert len(_FakeImprover.calls) == 1
    assert autopilot._improve_day_marker().is_file(), "當日 marker 落檔"
    # 模擬 execv 重啟:行程記憶體歸零,marker 仍在 → 同日不重跑
    monkeypatch.setattr(autopilot, "_project_improve_day", None)
    await _run_lane_once(monkeypatch)
    assert len(_FakeImprover.calls) == 1, "重啟後同日不得二度開場(首航 04:42 實證)"


def test_project_stale_in_progress_recovery(monkeypatch):
    import time as _t

    monkeypatch.setattr(autopilot.history, "busy_sessions", lambda *_a, **_k: [])
    monkeypatch.setattr(autopilot.history, "mark_interrupted", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "sweep_stale_running", lambda **k: None)
    meta = projects.create("回收測試", vision="v")
    sdir = projects.state_dir(meta["id"])
    dead = backlog.add("被腰斬的任務", state_dir=sdir)
    backlog.set_status(dead["id"], "in_progress", state_dir=sdir, session_id="pjdead")
    backlog.set_status(dead["id"], "in_progress", state_dir=sdir, updated_at=_t.time() - 3600)
    fresh = backlog.add("剛認領的任務", state_dir=sdir)
    backlog.set_status(fresh["id"], "in_progress", state_dir=sdir, session_id="pjfresh")
    autopilot._recover_stale_in_progress()
    by_id = {t["id"]: t for t in backlog.list_tasks(state_dir=sdir)}
    assert by_id[dead["id"]]["status"] == "pending", "死 session 的專案任務退回 pending"
    assert by_id[fresh["id"]]["status"] == "in_progress", "15 分內豁免(認領微窗不誤殺)"
