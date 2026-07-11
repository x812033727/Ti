"""execv 重載/優雅停機腰斬旁路調查時的收尾(_requeue_sideline_task)。

生產實證(2026-07-11 08:46):邊界 execv 腰斬旁路正在跑的 #490,任務留 in_progress 等
stale reaper,且 claim_next 的 attempts+1 白燒——重載一天數次,同一任務被斬幾次就會
被錯誤 parked。

守護不變量:
- 旁路任務退回 pending 且退還本輪 attempts(claim_next 的 +1);
- info=None(旁路閒置)與任務已終局(done)時 no-op,冪等;
- backlog 失敗吞掉不冒泡(不得影響 execv/停機路徑);
- _prepare_execv_reload 與 main 的 finally 都要接線,且 finally 中先收尾再 cancel。
"""

from __future__ import annotations

import inspect

import pytest

from studio import autopilot, backlog, config


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    return tmp_path


def _claimed(title: str) -> dict:
    backlog.add(title)
    t = backlog.claim_next(lambda _t: True)
    assert t is not None and t["attempts"] == 1
    return t


def test_requeue_refunds_attempts(monkeypatch):
    t = _claimed("調查:被腰斬")
    monkeypatch.setattr(autopilot, "_sideline_task_info", {"task_id": t["id"]})
    autopilot._requeue_sideline_task("execv 重載")
    got = next(x for x in backlog.list_tasks("pending") if x["id"] == t["id"])
    assert got["attempts"] == 0, "claim_next 的 attempts+1 必須退還"
    assert "退回重排" in got["note"]


def test_requeue_noop_when_idle_or_terminal(monkeypatch):
    t = _claimed("調查:已跑完")
    backlog.set_status(t["id"], "done", note="[調查結論] ok")
    monkeypatch.setattr(autopilot, "_sideline_task_info", {"task_id": t["id"]})
    autopilot._requeue_sideline_task("優雅停機")
    assert backlog.list_tasks("done")[0]["note"] == "[調查結論] ok", "已終局不得動"

    monkeypatch.setattr(autopilot, "_sideline_task_info", None)
    t2 = _claimed("主線任務不相干")
    autopilot._requeue_sideline_task("優雅停機")
    assert backlog.list_tasks("in_progress")[0]["id"] == t2["id"], "info=None 必須 no-op"


def test_requeue_swallows_backlog_failure(monkeypatch):
    t = _claimed("調查:退回時炸")
    monkeypatch.setattr(autopilot, "_sideline_task_info", {"task_id": t["id"]})
    monkeypatch.setattr(
        autopilot.backlog,
        "set_status",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    autopilot._requeue_sideline_task("execv 重載")  # 不拋即通過


def test_wired_into_execv_and_shutdown_paths():
    src_execv = inspect.getsource(autopilot._prepare_execv_reload)
    assert "_requeue_sideline_task" in src_execv, "execv 前必須收尾旁路(main finally 不會跑)"
    src_main = inspect.getsource(autopilot.main)
    i_requeue = src_main.find("_requeue_sideline_task")
    i_cancel = src_main.find("aux.cancel()")
    assert 0 <= i_requeue < i_cancel, "main finally 必須先收尾再 cancel(finally 會清 info)"
