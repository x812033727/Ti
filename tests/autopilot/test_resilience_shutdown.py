"""Autopilot 韌性：SIGTERM 優雅停機收尾、任務 timeout 改 parked、任務中心跳。

背景（一夜損失 ~7h 的三個根因）：
1. systemctl restart 直接 SIGTERM 殺行程——in-flight 任務無聲從零重跑、history meta
   永遠卡 running（網站無限顯示 ⏳ 執行中）。→ _graceful_shutdown_cleanup：退 pending
   自動重排、mark_interrupted、status.json state="stopped"。
2. 任務級 wait_for timeout 被標 failed 死路，無人分診。→ 主迴圈 TimeoutError 分支改標
   parked（需拆分或縮小範圍），backlog 分診看得見。
3. status.json 只在任務揀起時寫一次，長任務被外部監控誤判死鎖。→ _task_heartbeat 每
   ~60s 刷新 updated_at＋last_activity_at（session events 檔 mtime）。

停機收尾測 helper 本身（不發真訊號）；timeout→parked 走 main() 主迴圈（沿用
test_quota_gate 的 stub asyncio 模式）。
"""

from __future__ import annotations

import asyncio as real_asyncio
import contextlib
import json
import types

import pytest

from studio import autopilot, backlog, config, history


class _Stop(BaseException):
    """跳出 main() 無限迴圈用；繼承 BaseException 以免被 except Exception 吃掉。"""


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", d)
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "autopilot_paused", lambda: False)
    return d


def _read_status(state_dir):
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


# --- SIGTERM 優雅停機收尾（直接測 helper，不發真訊號）--------------------------


def test_graceful_cleanup_requeues_task_and_marks_session(state_dir):
    task = backlog.add("實作某功能")
    backlog.set_status(task["id"], "in_progress", session_id="ap-sig-1")
    history.start_session("ap-sig-1", "[autopilot] 實作某功能")

    autopilot._graceful_shutdown_cleanup(task["id"], "ap-sig-1")

    # backlog：退回 pending（服務重啟後自動重排，不再無聲從零重跑）
    t = backlog.list_tasks()[0]
    assert t["status"] == "pending"
    assert "服務重啟中斷" in t.get("note", "")
    # history：running meta 標 error（網站不再永遠顯示 ⏳ 執行中）
    meta = history.get_meta("ap-sig-1")
    assert meta["status"] == "error"
    assert "服務重啟中斷" in meta.get("note", "")
    # status.json：state="stopped"（外部監控辨識「主動停機」而非死鎖）
    status = _read_status(state_dir)
    assert status["state"] == "stopped"
    assert status["task_id"] == task["id"]


def test_graceful_cleanup_does_not_override_finished_session(state_dir):
    """mark_interrupted 冪等：已正常收尾的 meta 不得被停機收尾覆寫。"""
    task = backlog.add("實作某功能")
    history.start_session("ap-sig-2", "req")
    history.record_event(
        "ap-sig-2",
        {"type": "done", "session_id": "ap-sig-2", "ts": 0, "payload": {"completed": True}},
    )
    history.finish_session("ap-sig-2")

    autopilot._graceful_shutdown_cleanup(task["id"], "ap-sig-2")

    assert history.get_meta("ap-sig-2")["status"] == "completed"


def test_graceful_cleanup_single_step_failure_does_not_block_rest(state_dir, monkeypatch):
    """backlog 收尾炸掉也不得阻斷 history 標中斷與最終心跳（各步驟獨立容錯）。"""
    history.start_session("ap-sig-3", "req")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(autopilot.backlog, "set_status", boom)
    autopilot._graceful_shutdown_cleanup(99, "ap-sig-3")

    assert history.get_meta("ap-sig-3")["status"] == "error"
    assert _read_status(state_dir)["state"] == "stopped"


async def test_cancelled_run_one_task_with_shutdown_flag_cleans_up(
    state_dir, monkeypatch, tmp_path
):
    """停機旗標下取消 run_one_task：任務退 pending、meta 標 error（而非 finish_session 的
    incomplete）——這正是 systemctl restart 打斷 in-flight 任務的路徑。"""
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return str(clone)

    started = real_asyncio.Event()

    class HangingSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _requirement):
            started.set()
            await real_asyncio.Event().wait()  # 永不返回，等外部取消

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", HangingSession)
    task = backlog.add("跑到一半被 restart 的任務")
    backlog.set_status(task["id"], "in_progress")

    runner_task = real_asyncio.create_task(autopilot.run_one_task(task))
    await started.wait()
    sid = backlog.list_tasks()[0]["session_id"]  # run_one_task 已寫回 session_id
    assert history.get_meta(sid)["status"] == "running"

    monkeypatch.setattr(autopilot, "_shutdown_requested", True)
    runner_task.cancel()
    with pytest.raises(real_asyncio.CancelledError):
        await runner_task

    assert backlog.list_tasks()[0]["status"] == "pending"
    meta = history.get_meta(sid)
    assert meta["status"] == "error", (
        "停機路徑須 mark_interrupted，不得被 finish_session 蓋成 incomplete"
    )
    assert _read_status(state_dir)["state"] == "stopped"


async def test_cancelled_run_one_task_without_flag_does_not_requeue(
    state_dir, monkeypatch, tmp_path
):
    """非停機的取消（旗標未設）不得誤入停機收尾：維持原行為（finish_session 收尾）。"""
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return str(clone)

    started = real_asyncio.Event()

    class HangingSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _requirement):
            started.set()
            await real_asyncio.Event().wait()

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", HangingSession)
    task = backlog.add("被其他原因取消的任務")
    backlog.set_status(task["id"], "in_progress")

    runner_task = real_asyncio.create_task(autopilot.run_one_task(task))
    await started.wait()
    runner_task.cancel()
    with pytest.raises(real_asyncio.CancelledError):
        await runner_task

    assert backlog.list_tasks()[0]["status"] == "in_progress", "非停機取消不得退回 pending"


# --- 任務 timeout → parked（主迴圈分支）---------------------------------------


def _stub_asyncio(monkeypatch, sleeps):
    """把 autopilot 模組內的 asyncio 換成 stub：sleep 記錄秒數後丟 _Stop 跳出迴圈。"""

    async def fake_sleep(s):
        sleeps.append(s)
        raise _Stop

    monkeypatch.setattr(
        autopilot,
        "asyncio",
        types.SimpleNamespace(to_thread=real_asyncio.to_thread, sleep=fake_sleep),
    )


async def test_main_loop_task_timeout_marks_parked_not_failed(state_dir, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(config, "AUTOPILOT_TASK_TIMEOUT", 123)
    monkeypatch.setattr(autopilot, "_install_signal_handlers", lambda: None)
    task = backlog.add("超大的任務")

    async def timeout_run(_task):
        raise TimeoutError("autopilot task timeout after 123s")

    monkeypatch.setattr(autopilot, "run_one_task", timeout_run)
    sleeps: list[float] = []
    _stub_asyncio(monkeypatch, sleeps)

    with pytest.raises(_Stop):
        await autopilot.main()

    t = backlog.list_tasks()[0]
    assert t["status"] == "parked", "任務 timeout 應標 parked 供分診，不再 failed 死路"
    assert "task timeout after 123s" in t.get("note", "")
    assert "需拆分或縮小範圍" in t.get("note", "")
    assert task["id"] == t["id"]


async def test_main_loop_other_exception_still_marks_failed(state_dir, monkeypatch):
    """非 timeout 的例外維持原行為：標 failed（timeout→parked 不得影響其他失敗分類）。"""
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(autopilot, "_install_signal_handlers", lambda: None)
    backlog.add("會炸的任務")

    async def broken_run(_task):
        raise RuntimeError("boom")

    monkeypatch.setattr(autopilot, "run_one_task", broken_run)
    _stub_asyncio(monkeypatch, [])

    with pytest.raises(_Stop):
        await autopilot.main()

    t = backlog.list_tasks()[0]
    assert t["status"] == "failed"
    assert "RuntimeError" in t.get("note", "")


# --- 任務中心跳 ----------------------------------------------------------------


async def test_task_heartbeat_refreshes_status_and_keeps_fields(state_dir, monkeypatch):
    monkeypatch.setattr(autopilot, "_HEARTBEAT_INTERVAL_S", 0.01)
    history.start_session("ap-hb-1", "req")
    autopilot._write_status("running", task_id=7, quota={"claude": 12})
    before = _read_status(state_dir)

    hb = real_asyncio.create_task(autopilot._task_heartbeat(7, "ap-hb-1"))
    await real_asyncio.sleep(0.05)
    hb.cancel()
    with contextlib.suppress(real_asyncio.CancelledError):
        await hb

    status = _read_status(state_dir)
    assert status["state"] == "running"
    assert status["task_id"] == 7
    assert status["quota"] == {"claude": 12}, "心跳須保留既有 quota 欄位"
    assert status["updated_at"] > before["updated_at"], "心跳須刷新 updated_at"
    assert status["last_activity_at"] == pytest.approx(history.events_mtime("ap-hb-1"), abs=1), (
        "last_activity_at 應為 session events 檔 mtime"
    )


async def test_task_heartbeat_no_events_file_reports_none(state_dir, monkeypatch):
    monkeypatch.setattr(autopilot, "_HEARTBEAT_INTERVAL_S", 0.01)
    hb = real_asyncio.create_task(autopilot._task_heartbeat(3, "no-such-session"))
    await real_asyncio.sleep(0.05)
    hb.cancel()
    with contextlib.suppress(real_asyncio.CancelledError):
        await hb

    status = _read_status(state_dir)
    assert status["task_id"] == 3
    assert status["last_activity_at"] is None


def test_status_helpers_roundtrip(state_dir):
    """_read_status 讀回 _write_status 寫入的欄位；檔案缺失/壞 JSON 回空 dict。"""
    assert autopilot._read_status() == {}
    autopilot._write_status("idle", quota={"claude": 1})
    assert autopilot._read_status()["state"] == "idle"
    (state_dir / "status.json").write_text("{壞 json", encoding="utf-8")
    assert autopilot._read_status() == {}


def test_write_status_stopped_includes_last_activity_field(state_dir):
    """status.json 契約：新增 last_activity_at 欄位恆存在（無資訊時為 None）。"""
    autopilot._write_status("stopped", task_id=1)
    status = _read_status(state_dir)
    assert status["state"] == "stopped"
    assert "last_activity_at" in status and status["last_activity_at"] is None
