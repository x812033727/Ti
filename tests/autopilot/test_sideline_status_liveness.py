"""旁路心跳不得蓋寫主迴圈的 status.json 身分欄位(2026-07-11 生產實測)。

事故:旁路 _run_investigation_task 啟動的 _task_heartbeat 每 60s 把 status.json 的
task_id 蓋成旁路任務,與主迴圈心跳乒乓(看板 main 顯示成 sideline 的 #457);state
也被蓋回 running,主迴圈 quota_sleep/paused 期間會閃爍。

守護不變量:
- liveness_only=True 保留 prev 的 state/task_id,只刷活性欄位(updated_at/
  last_activity_at/workers);
- 預設(主迴圈模式)行為不變:認領 task_id、state=running;
- 旁路呼叫 _run_investigation_task 必須帶 sideline=True(源碼守門)。
"""

from __future__ import annotations

import inspect
import json

import pytest

from studio import autopilot, config


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    return tmp_path / "ap"


def _status(state_dir) -> dict:
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


def test_liveness_only_preserves_main_identity(state_dir):
    autopilot._write_status("running", task_id=422)
    before = _status(state_dir)
    autopilot._write_running_status_preserving(
        457, liveness_only=True, last_activity_at=before["updated_at"] + 5
    )
    after = _status(state_dir)
    assert after["task_id"] == 422, "旁路活性刷新不得把主任務蓋成旁路任務"
    assert after["state"] == "running"
    assert after["last_activity_at"] == before["updated_at"] + 5, "活性欄位要真的有刷"


def test_liveness_only_preserves_non_running_state(state_dir):
    autopilot._write_status("quota_sleep", task_id=None, sleep_until=9e9)
    autopilot._write_running_status_preserving(457, liveness_only=True)
    after = _status(state_dir)
    assert after["state"] == "quota_sleep", "主迴圈 quota_sleep 不得被旁路心跳蓋回 running"
    assert after["sleep_until"] == 9e9


def test_default_mode_still_claims_identity(state_dir):
    autopilot._write_status("idle", task_id=None)
    autopilot._write_running_status_preserving(457)
    after = _status(state_dir)
    assert after["task_id"] == 457 and after["state"] == "running", "主迴圈模式行為不得回歸"


def test_sideline_call_passes_flag_and_heartbeat_wires_it():
    src = inspect.getsource(autopilot._investigation_sideline)
    assert "sideline=True" in src, "旁路呼叫 _run_investigation_task 必須帶 sideline=True"
    src_inv = inspect.getsource(autopilot._run_investigation_task)
    assert "liveness_only=sideline" in src_inv, "調查管線心跳必須把 sideline 接到 liveness_only"
