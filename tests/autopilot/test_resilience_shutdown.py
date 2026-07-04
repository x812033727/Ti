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
    task = backlog.add("實作某功能")
    backlog.set_status(task["id"], "in_progress")
    history.start_session("ap-sig-3", "req")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(autopilot.backlog, "set_status", boom)
    autopilot._graceful_shutdown_cleanup(task["id"], "ap-sig-3")

    assert history.get_meta("ap-sig-3")["status"] == "error"
    assert _read_status(state_dir)["state"] == "stopped"


def test_graceful_cleanup_preserves_recorded_outcome(state_dir):
    """冪等護欄：任務已寫下最終結果（如閘門放棄的 failed）→ 停機收尾不得把它復活成 pending。"""
    task = backlog.add("已放棄的任務")
    backlog.set_status(task["id"], "failed", note="[test] 連續 3 次未過，放棄")
    history.start_session("ap-sig-4", "req")

    autopilot._graceful_shutdown_cleanup(task["id"], "ap-sig-4")

    assert backlog.list_tasks()[0]["status"] == "failed", "既定結果不得被停機收尾覆蓋"
    assert history.get_meta("ap-sig-4")["status"] == "error"  # meta 收尾照做
    assert _read_status(state_dir)["state"] == "stopped"


def test_set_status_if_in_progress_guard(state_dir):
    """_set_status_if_in_progress：只救 in_progress；pending/failed/缺席一律不寫。"""
    t1 = backlog.add("進行中")
    backlog.set_status(t1["id"], "in_progress")
    assert autopilot._set_status_if_in_progress(t1["id"], "pending", note="n") is True
    assert backlog.list_tasks()[0]["status"] == "pending"
    # 已是 pending：再呼叫不寫（冪等、不疊 note）
    assert autopilot._set_status_if_in_progress(t1["id"], "done") is False
    assert backlog.list_tasks()[0]["status"] == "pending"
    # 任務不存在
    assert autopilot._set_status_if_in_progress(9999, "pending") is False


# --- merge 感知的停機收尾（merge 後中斷收斂 done，不重跑）-----------------------


def test_finalize_merged_task_converges_to_done(state_dir):
    """merge 已成功後被停機打斷 → 收斂 done（帶追溯欄位），絕不退回 pending 重跑重開 PR。"""
    task = backlog.add("已合併但沒收尾的任務")
    backlog.set_status(task["id"], "in_progress", session_id="ap-mg-1")
    history.start_session("ap-mg-1", "req")

    autopilot._shutdown_finalize_task(
        task["id"],
        "ap-mg-1",
        merged=True,
        done_fields={"pr": 77, "merged_branch": "autopilot/task-x"},
    )

    t = backlog.list_tasks()[0]
    assert t["status"] == "done"
    assert t["pr"] == 77 and t["merged_branch"] == "autopilot/task-x"
    assert "已進 main" in t.get("note", "")
    assert history.get_meta("ap-mg-1")["status"] == "error"  # meta 標中斷（冪等）
    assert _read_status(state_dir)["state"] == "stopped"


def test_finalize_not_merged_requeues(state_dir):
    """merge 尚未成功 → 走原退回 pending 語意。"""
    task = backlog.add("跑到一半的任務")
    backlog.set_status(task["id"], "in_progress")
    autopilot._shutdown_finalize_task(task["id"], None, merged=False)
    assert backlog.list_tasks()[0]["status"] == "pending"


def test_finalize_merged_preserves_recorded_outcome(state_dir):
    """merge 後重佈失敗已標 failed → 停機收斂不得把 failed 改寫成 done。"""
    task = backlog.add("重佈失敗的任務")
    backlog.set_status(task["id"], "failed", note="重佈失敗已自動回滾")
    autopilot._shutdown_finalize_task(task["id"], None, merged=True, done_fields={"pr": 1})
    assert backlog.list_tasks()[0]["status"] == "failed"


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


# --- 審查修正（commit 3）：SIGTERM 競態三層防護、心跳例外隔離、execv 訊號安全 ----


def test_read_status_bad_encoding_returns_empty(state_dir):
    """#2 回歸：status.json 壞編碼（UnicodeDecodeError ∈ ValueError）不得外洩炸掉心跳。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "status.json").write_bytes(b"\xff\xfe\x00 broken")
    assert autopilot._read_status() == {}


def test_repeated_sigterm_reissues_cancel(monkeypatch):
    """#1c 回歸：重複 SIGTERM 必須能再度 cancel（首次取消可能在競態中被吞）。"""
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)

    class FakeTask:
        def __init__(self):
            self.cancels = 0

        def done(self):
            return False

        def cancel(self):
            self.cancels += 1

    t = FakeTask()
    autopilot._request_shutdown("SIGTERM", t)
    autopilot._request_shutdown("SIGTERM", t)
    assert t.cancels == 2, "重複訊號不得被去重吞掉"
    assert autopilot._shutdown_requested is True


async def test_main_loop_top_guard_exits_when_flag_set(state_dir, monkeypatch):
    """#1b 回歸：旗標已立、取消卻被吞——迴圈頂端兜底立即走停機路徑，不再取新任務。"""
    monkeypatch.setattr(autopilot, "_shutdown_requested", True)
    monkeypatch.setattr(autopilot, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: pytest.fail("停機中不得取任務"))

    await autopilot.main()  # 應乾淨返回（CancelledError 被 main 收斂），而非取任務

    assert _read_status(state_dir)["state"] == "stopped"


async def test_shutdown_cancel_eaten_at_heartbeat_join_still_shuts_down(
    state_dir, monkeypatch, tmp_path
):
    """#1a 核心回歸（審查實測情境）：SIGTERM 的取消恰在 finally 的 `await heartbeat`
    送達——與心跳自身的 CancelledError 無法區分而被吃掉。旗標＋cancelling() 兜底必須
    補收尾並重新拋出取消，否則停機遺失、迴圈續跑到被 SIGKILL。"""
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return str(clone)

    class QuickSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _requirement):
            # 立即返回 provider_unavailable：run_one_task 隨後全是同步碼，直到 finally 的
            # await heartbeat 才有第一個（也是最後一個）可送達取消的 await 點。
            return {"completed": False, "provider_unavailable": "codex"}

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", QuickSession)
    task = backlog.add("停機競態下的任務")

    holder: dict = {}
    real_set_status = backlog.set_status

    def set_status_then_sigterm(task_id, status, **kw):
        out = real_set_status(task_id, status, **kw)
        if status == "pending":  # provider_unavailable 收尾點：模擬此刻收到 SIGTERM
            autopilot._request_shutdown("SIGTERM", holder["task"])
        return out

    monkeypatch.setattr(autopilot.backlog, "set_status", set_status_then_sigterm)

    runner_task = real_asyncio.create_task(autopilot.run_one_task(task))
    holder["task"] = runner_task
    with pytest.raises(real_asyncio.CancelledError):
        await runner_task  # 取消不得被 heartbeat join 吞掉

    assert backlog.list_tasks()[0]["status"] == "pending"  # 既定結果保留（護欄不覆蓋）
    assert _read_status(state_dir)["state"] == "stopped"  # 停機收尾已補做


async def test_heartbeat_crash_does_not_replace_task_outcome(state_dir, monkeypatch, tmp_path):
    """#2 回歸：心跳背景任務炸掉（如壞編碼）→ 收尾 join 只 log 不外拋，任務結果不受影響。"""
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return str(clone)

    class QuickSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _requirement):
            return {"completed": False, "provider_unavailable": "codex"}

    async def broken_heartbeat(_task_id, _sid):
        raise RuntimeError("heartbeat 炸了")

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", QuickSession)
    monkeypatch.setattr(autopilot, "_task_heartbeat", broken_heartbeat)
    task = backlog.add("心跳炸掉但任務正常的案例")

    await autopilot.run_one_task(task)  # 不得把 RuntimeError 外拋成任務失敗

    assert backlog.list_tasks()[0]["status"] == "pending"  # provider_unavailable 的正常收尾


async def test_shutdown_during_gates_requeues(state_dir, monkeypatch, tmp_path):
    """#4 回歸：SIGTERM 落在閘門階段（session.run 已結束）→ 仍要收尾退 pending，
    不得留 in_progress 無聲重跑。"""
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return str(clone)

    class OkSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _requirement):
            return {"completed": True}

    gate_started = real_asyncio.Event()

    async def hanging_gate(_clone):
        gate_started.set()
        await real_asyncio.Event().wait()

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", OkSession)
    monkeypatch.setattr(autopilot, "_gate_lint", hanging_gate)
    task = backlog.add("閘門中被停機的任務")

    runner_task = real_asyncio.create_task(autopilot.run_one_task(task))
    await gate_started.wait()
    monkeypatch.setattr(autopilot, "_shutdown_requested", True)
    runner_task.cancel()
    with pytest.raises(real_asyncio.CancelledError):
        await runner_task

    assert backlog.list_tasks()[0]["status"] == "pending"
    assert _read_status(state_dir)["state"] == "stopped"


async def test_shutdown_after_merge_converges_done(state_dir, monkeypatch, tmp_path):
    """#4 核心回歸：merge 已成功、重佈等待中被 SIGTERM → 收斂 done（帶追溯欄位），
    絕不退回 pending 對同一份已合併成果重跑重開 PR。"""
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return str(clone)

    class OkSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _requirement):
            return {"completed": True}

    async def gate_ok(_clone):
        return True, "ok"

    async def merge_ok(_clone, _task):
        return autopilot.MergeResult(True, "已合併", pr_number=42, branch="autopilot/task-z")

    idle_started = real_asyncio.Event()

    async def hanging_idle(*_a, **_k):
        idle_started.set()
        await real_asyncio.Event().wait()

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", OkSession)
    monkeypatch.setattr(autopilot, "_gate_lint", gate_ok)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", gate_ok)
    monkeypatch.setattr(autopilot, "_gate_tests", gate_ok)
    monkeypatch.setattr(autopilot, "_commit_push_merge", merge_ok)
    monkeypatch.setattr(autopilot, "_wait_until_idle", hanging_idle)
    task = backlog.add("merge 後被停機的任務")

    runner_task = real_asyncio.create_task(autopilot.run_one_task(task))
    await idle_started.wait()
    monkeypatch.setattr(autopilot, "_shutdown_requested", True)
    runner_task.cancel()
    with pytest.raises(real_asyncio.CancelledError):
        await runner_task

    t = backlog.list_tasks()[0]
    assert t["status"] == "done", "已合併的成果不得重跑（會重開 PR），應收斂 done"
    assert t["pr"] == 42 and t["merged_branch"] == "autopilot/task-z"
    assert _read_status(state_dir)["state"] == "stopped"


async def test_prepare_execv_reload_removes_signal_handlers():
    """#3 回歸：execv 前卸下 SIGTERM/SIGINT handler（回復預設處置）並讓出一個 tick。"""
    import signal as signal_mod

    loop = real_asyncio.get_running_loop()
    loop.add_signal_handler(signal_mod.SIGTERM, lambda: None)
    assert signal_mod.getsignal(signal_mod.SIGTERM) is not signal_mod.SIG_DFL

    await autopilot._prepare_execv_reload()

    assert signal_mod.getsignal(signal_mod.SIGTERM) is signal_mod.SIG_DFL, (
        "execv 前必須回復預設訊號處置，晚到的 SIGTERM 才能直接終止行程"
    )


async def test_main_loop_reload_aborts_execv_on_shutdown(state_dir, monkeypatch):
    """#3 回歸：已排入的停機請求要能中止 execv 路徑（prep 的讓步 tick 讓 cancel 先跑）。"""
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)
    monkeypatch.setattr(autopilot, "_install_signal_handlers", lambda: None)
    sigs = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr(autopilot, "_self_sig", lambda: next(sigs))  # 觸發 reload 分支
    backlog.add("跑一輪就好")

    async def quick_run(_task):
        return None

    async def prep_with_queued_shutdown():
        # 模擬「SIGTERM callback 已排入、於讓步 tick 執行」：設旗標並拋出取消。
        autopilot._shutdown_requested = True
        raise real_asyncio.CancelledError()

    execved: list = []
    monkeypatch.setattr(autopilot, "run_one_task", quick_run)
    monkeypatch.setattr(autopilot, "_prepare_execv_reload", prep_with_queued_shutdown)
    monkeypatch.setattr(autopilot.os, "execv", lambda *a: execved.append(a))

    await autopilot.main()  # 取消被 main 收斂成優雅停機

    assert execved == [], "停機請求已排入時不得 execv"
    assert _read_status(state_dir)["state"] == "stopped"
