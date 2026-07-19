"""調查旁路併行(吞吐強化 δ,預設關)+ backlog.claim_next 原子認領。

背景:主迴圈單 worker,吞吐 ~8 任務/天、pending 積壓;調查任務 ~89s(37% pending 符合)
卻要排隊等 ~51min 的完整管線任務。旁路線併行消化,免費多一條吞吐通道。

守護不變量:
- claim_next:單一 flock 內認領(兩協程併發不重複);priority/created_at 排序與
  next_pending 一致;attempts+1 語意;無符合回 None。
- sideline:旋鈕關(預設)/lane 關/暫停/額度受限 → 不取任務;認領後走
  _run_investigation_task(獨立 -inv clone);例外不冒泡;sideline 子欄寫入與清除。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, backlog, config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


# --- claim_next ---------------------------------------------------------------


def test_claim_next_order_and_attempts():
    backlog.add("低優先", priority=2)
    hi = backlog.add("高優先", priority=0)
    got = backlog.claim_next(lambda t: True)
    assert got["id"] == hi["id"], "priority 排序與 next_pending 一致"
    assert (
        got["status"] == "in_progress" and got["attempts"] == 1
    ), "認領即標 in_progress+attempts+1"


def test_claim_next_predicate_and_none():
    backlog.add("普通任務甲")
    assert (
        backlog.claim_next(lambda t: "調查" in t["title"]) is None
    ), "無符合回 None 且不動任何任務"
    assert backlog.list_tasks("pending")[0]["title"] == "普通任務甲"


@pytest.mark.asyncio
async def test_claim_next_no_double_claim():
    """兩協程併發 claim 同一 predicate:同一筆任務只會被認領一次。"""
    t = backlog.add("調查唯一任務")
    results = await asyncio.gather(
        asyncio.to_thread(backlog.claim_next, lambda x: True),
        asyncio.to_thread(backlog.claim_next, lambda x: True),
    )
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1 and claimed[0]["id"] == t["id"], f"重複認領:{results}"


# --- sideline 閘門 --------------------------------------------------------------


async def _run_sideline_once(monkeypatch):
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await autopilot._investigation_sideline()


@pytest.fixture
def sideline_on(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_PARALLEL", True)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", True)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)  # 測試不打額度快照
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)
    monkeypatch.setattr(autopilot, "_sideline_task_info", None)


@pytest.mark.asyncio
async def test_sideline_claims_and_runs_investigation(monkeypatch, sideline_on, tmp_path):
    t = backlog.add("調查 X 的根因並回報")
    ran: list = []

    async def fake_clone(work_dir=None):
        assert str(work_dir).endswith("-inv"), "旁路必須用獨立 -inv clone"
        return str(tmp_path / "inv")

    async def fake_run(task, clone, sid, t0, *, sideline=False):
        assert sideline is True, "旁路呼叫必須帶 sideline=True(心跳 liveness_only)"
        ran.append((task["id"], autopilot._sideline_task_info))
        backlog.set_status(task["id"], "done", note="[調查結論] ok")

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_clone)
    monkeypatch.setattr(autopilot, "_run_investigation_task", fake_run)

    await _run_sideline_once(monkeypatch)

    assert ran and ran[0][0] == t["id"]
    assert ran[0][1]["task_id"] == t["id"], "執行中 sideline 子欄須帶任務資訊"
    assert autopilot._sideline_task_info is None, "跑完清除子欄"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["knob_off", "lane_off", "paused", "quota_limited"])
async def test_sideline_respects_gates(monkeypatch, sideline_on, gate):
    backlog.add("調查 X 的根因並回報")
    if gate == "knob_off":
        monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_PARALLEL", False)
    elif gate == "lane_off":
        monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", False)
    elif gate == "paused":
        monkeypatch.setattr(config, "autopilot_paused", lambda: True)
    elif gate == "quota_limited":
        monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", True)
        from studio import provider_quota

        monkeypatch.setattr(provider_quota, "snapshot", lambda: {})
        monkeypatch.setattr(provider_quota, "gate", lambda snap: (False, None))

    claimed: list = []
    monkeypatch.setattr(autopilot.backlog, "claim_next", lambda p, **k: claimed.append(1) or None)
    await _run_sideline_once(monkeypatch)
    assert not claimed, f"{gate} 時不得取任務"


@pytest.mark.asyncio
async def test_sideline_exception_does_not_propagate(monkeypatch, sideline_on):
    backlog.add("調查 X 的根因並回報")

    async def boom_clone(work_dir=None):
        raise RuntimeError("clone exploded")

    monkeypatch.setattr(autopilot, "_prepare_clone", boom_clone)
    await _run_sideline_once(monkeypatch)  # 不拋(CancelledError 除外)即通過
    assert autopilot._sideline_task_info is None
