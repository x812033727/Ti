"""backlog 手動操作 apply_action(功能強化 C1)。

守護不變量:
- retry(failed/parked→pending)與 unpark(parked→pending)歸零 attempts——parked/failed
  多為 attempts 燒滿的歸檔,不歸零會被 AUTOPILOT_TASK_MAX_ATTEMPTS 閘門立即再判死。
- park 對 in_progress/merging 回「不可」(409 語意)——進行中任務狀態機由 runner/reconciler
  持有,人工改寫會互相踩踏。
- priority 只改欄位不動 status/attempts;夾 0-2;updated_at 更新。
- 錯誤訊息前綴契約:「不支援」→400、「不存在」→404、「不可」→409(routes 據此映射)。
"""

from __future__ import annotations

import pytest

from studio import backlog, config, routes


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def _mk(status="pending", attempts=0):
    t = backlog.add(f"任務-{status}-{attempts}")
    fields = {"attempts": attempts} if attempts else {}
    if status != "pending":
        backlog.set_status(t["id"], status, **fields)
    elif fields:
        backlog.set_status(t["id"], "pending", **fields)
    return t


def test_retry_resets_attempts():
    t = _mk("failed", attempts=3)
    task, err = backlog.apply_action(t["id"], "retry")
    assert err == "" and task["status"] == "pending" and task["attempts"] == 0
    assert task["note"].startswith("[手動]")


def test_unpark_resets_attempts():
    t = _mk("parked", attempts=2)
    task, err = backlog.apply_action(t["id"], "unpark", note="值得再試")
    assert err == "" and task["status"] == "pending" and task["attempts"] == 0
    assert "值得再試" in task["note"]


@pytest.mark.parametrize("status", ["in_progress", "merging"])
def test_park_blocked_for_active_states(status):
    t = _mk(status)
    task, err = backlog.apply_action(t["id"], "park")
    assert task is None and err.startswith("不可")


def test_park_pending_ok():
    t = _mk("pending")
    task, err = backlog.apply_action(t["id"], "park")
    assert err == "" and task["status"] == "parked"


def test_priority_only_changes_priority():
    t = _mk("in_progress")
    before = next(x for x in backlog.list_tasks() if x["id"] == t["id"])
    task, err = backlog.apply_action(t["id"], "priority", priority=9)
    assert err == "" and task["priority"] == 2, "夾 0-2"
    assert task["status"] == before["status"], "不動 status"
    assert task.get("attempts", 0) == before.get("attempts", 0), "不動 attempts"
    assert task["updated_at"] >= before["updated_at"]


def test_error_prefix_contract():
    assert backlog.apply_action(999, "retry")[1].startswith("不存在")
    t = _mk("pending")
    assert backlog.apply_action(t["id"], "explode")[1].startswith("不支援")
    assert backlog.apply_action(t["id"], "priority")[1].startswith("不支援"), "缺 priority 欄位"
    assert backlog.apply_action(t["id"], "retry")[1].startswith("不可"), "pending 不可 retry"


# --- API 映射 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_status_code_mapping():
    t = _mk("failed")
    ok = await routes.autopilot_task_action(t["id"], routes.TaskActionBody(action="retry"))
    assert ok.status_code == 200

    nf = await routes.autopilot_task_action(999, routes.TaskActionBody(action="retry"))
    assert nf.status_code == 404

    t2 = _mk("in_progress")
    conflict = await routes.autopilot_task_action(t2["id"], routes.TaskActionBody(action="park"))
    assert conflict.status_code == 409

    bad = await routes.autopilot_task_action(t2["id"], routes.TaskActionBody(action="explode"))
    assert bad.status_code == 400


def test_action_endpoint_requires_admin():
    """寫入端點必須掛 WRITE_DEPS(admin gate),防未授權操作生產 backlog。"""
    route = next(
        r
        for r in routes.router.routes
        if getattr(r, "path", "") == "/api/autopilot/task/{task_id}/action"
    )
    dep_names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
    assert "require_admin" in dep_names
