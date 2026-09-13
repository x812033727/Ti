"""每日 PR 成本熔斷（TI_AUTOPILOT_DAILY_PR_BUDGET）黑白樣本守護。

契約：budget<=0 恆放行（預設行為不變）；未超限照常 push＋開 PR；超限時
`_commit_push_merge` 回 (False, 含「每日 PR 預算」) 且不執行任何 git/gh 副作用；
計數只算 UTC「當日」且「實際開出 PR（pr 非空）」的 audit 紀錄，跨日自動歸零；
run_one_task 在 merge 前超限 → 任務退回 pending 且不消耗 attempts（對照
_handle_gate_failure 會 +1 的黑樣本）。
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from studio import autopilot, backlog, config, history, publisher

_TASK = {"id": "7", "title": "budget guard", "detail": "", "attempts": 0}
_AUTOPILOT_REPO = "core/autopilot"


class RunSpy:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{_AUTOPILOT_REPO}.git")
        if "rev-list --count" in joined:
            return (0, "1")
        if "rev-parse HEAD" in joined:
            return (0, "abc1234")
        if "pr view" in joined:
            return (0, "42")
        return (0, "")

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def called(self, fragment: str) -> bool:
        return any(fragment in j for j in self.joined())


class StopLoop(BaseException):
    """跳出 `_main_loop()` 無限迴圈用，避免被 `except Exception` 吃掉。"""


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _AUTOPILOT_REPO)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"core"}))
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 0)
    monkeypatch.setattr(autopilot, "_daily_budget_notified_day", None)


def _seed_audit(n: int, *, ts: float | None = None, pr: int | None = 1) -> None:
    """種 n 筆 audit 紀錄（預設今日、有 PR）。"""
    for i in range(n):
        autopilot._append_audit(
            {"ts": ts if ts is not None else time.time(), "task_id": i, "pr": pr}
        )


async def _merged(number, payload, **kwargs):
    return (publisher.MergeOutcome.MERGED, "merged")


# --- 計數口徑 ---------------------------------------------------------------


def test_budget_zero_never_exceeded():
    _seed_audit(50)
    assert autopilot._daily_pr_budget_exceeded() is False  # 0＝不限制


def test_count_only_today_with_pr(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 2)
    _seed_audit(2, ts=time.time() - 86400)  # 昨日：不計
    _seed_audit(1, pr=None)  # 今日但沒開到 PR：不計
    assert autopilot._todays_pr_count() == 0
    assert autopilot._daily_pr_budget_exceeded() is False

    _seed_audit(2)  # 今日有 PR：計入
    assert autopilot._todays_pr_count() == 2
    assert autopilot._daily_pr_budget_exceeded() is True  # 2 >= 2


def test_corrupt_lines_skipped():
    _seed_audit(1)
    with autopilot._audit_path().open("a", encoding="utf-8") as f:
        f.write("{ 不是 JSON\n")
        f.write('{"ts": "not-a-number", "pr": 1}\n')
    assert autopilot._todays_pr_count() == 1  # 壞行跳過不炸


@pytest.mark.asyncio
async def test_main_loop_budget_sleep_notifies_once_per_utc_day(monkeypatch, tmp_path):
    """主迴圈預算睡眠會送 webhook，且同一 UTC 日只送一次。"""

    async def _admission_ok():
        return True

    async def _no_admission_circuit():
        return False

    async def _sleep_once(delay):
        sleeps.append(delay)
        raise StopLoop

    sleeps: list[float] = []
    sent: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 1)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_MAX_SLEEP", 1800)
    monkeypatch.setattr(config, "autopilot_paused", lambda: False)
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)
    monkeypatch.setattr(autopilot, "_maybe_apply_pinned_account", lambda: None)
    monkeypatch.setattr(autopilot, "_reconcile_admission_mode_boundary", _admission_ok)
    monkeypatch.setattr(autopilot, "_note_resumed", lambda: None)
    monkeypatch.setattr(autopilot, "_maybe_enqueue_schedules", lambda: None)
    monkeypatch.setattr(autopilot, "_maybe_pause_for_admission_circuit", _no_admission_circuit)
    monkeypatch.setattr(autopilot.autonomy, "policy_exists", lambda _project_id: False)
    monkeypatch.setattr(autopilot, "_daily_pr_budget_exceeded", lambda: True)
    monkeypatch.setattr(autopilot, "_todays_pr_count", lambda _now=None: 1)
    monkeypatch.setattr(autopilot, "_next_utc_midnight", lambda now: now + 600)
    monkeypatch.setattr(
        autopilot.backlog,
        "next_pending",
        lambda: pytest.fail("budget_sleep 時不得取任務"),
    )
    monkeypatch.setattr(autopilot.asyncio, "sleep", _sleep_once)
    monkeypatch.setattr(
        autopilot.notify,
        "send_bg",
        lambda kind, title, **kw: sent.append((kind, title, kw)),
    )
    monkeypatch.setattr(
        autopilot.time,
        "gmtime",
        lambda _now=None: (2026, 7, 20, 1, 0, 0, 0, 0, 0),
    )

    for _ in range(2):
        with pytest.raises(StopLoop):
            await autopilot._main_loop(startup_sig=0.0)

    assert sleeps == [pytest.approx(600), pytest.approx(600)]
    assert [item[0] for item in sent] == ["budget_trip"]
    assert sent[0][2] == {"used": 1, "cap": 1}
    status = json.loads((tmp_path / "state" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "budget_sleep"


# --- _commit_push_merge 兜底 guard（黑白樣本）--------------------------------


@pytest.mark.asyncio
async def test_under_budget_proceeds(monkeypatch):
    """白樣本：預算 5、今日 3 → 照常 push＋開 PR。"""
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 5)
    _seed_audit(3)
    spy = RunSpy()
    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _merged)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert spy.called("push")
    assert spy.called(f"pr create -R {_AUTOPILOT_REPO}")


@pytest.mark.asyncio
async def test_over_budget_aborts_without_side_effects(monkeypatch):
    """黑樣本：預算 2、今日 2 → 中止且零 git/gh 副作用。"""
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 2)
    _seed_audit(2)
    run = AsyncMock()
    merge_flow = AsyncMock()
    monkeypatch.setattr(autopilot, "_run", run)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "每日 PR 預算" in msg
    run.assert_not_awaited()
    merge_flow.assert_not_awaited()
    assert publisher.current_repo() == ""  # 覆寫未生效、未殘留


@pytest.mark.asyncio
async def test_yesterday_records_reset_across_day(monkeypatch):
    """跨日重置：昨日已滿額，今日照常放行。"""
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 2)
    _seed_audit(2, ts=time.time() - 86400)
    spy = RunSpy()
    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _merged)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert spy.called("push")


@pytest.mark.asyncio
async def test_dryrun_not_limited(monkeypatch):
    """dryrun 不打真 PR，不受預算限制。"""
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 1)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    _seed_audit(5)
    spy = RunSpy()
    monkeypatch.setattr(autopilot, "_run", spy)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True
    assert "[dryrun]" in msg


# --- run_one_task 超限退回 pending（不消耗 attempts）--------------------------


def _patch_run_one_task_machinery(monkeypatch, tmp_path):
    """把 run_one_task 的重機具（clone/session/閘門）換成假件，聚焦預算分流。"""

    async def _fake_clone():
        return str(tmp_path / "clone")

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def run(self, requirement):
            return {"completed": True}

    async def _gate_ok(clone):
        return (True, "")

    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    monkeypatch.setattr(autopilot, "StudioSession", _FakeSession)
    monkeypatch.setattr(autopilot, "_gate_lint", _gate_ok)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", _gate_ok)
    monkeypatch.setattr(autopilot, "_gate_tests", _gate_ok)


@pytest.mark.asyncio
async def test_run_one_task_over_budget_bounces_to_pending_without_attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 1)
    _seed_audit(1)
    _patch_run_one_task_machinery(monkeypatch, tmp_path)
    merge = AsyncMock()
    monkeypatch.setattr(autopilot, "_commit_push_merge", merge)

    task = backlog.add("超限任務")
    await autopilot.run_one_task(task)

    merge.assert_not_awaited()  # merge 前就被擋，不進 _commit_push_merge
    after = backlog.list_tasks()[0]
    assert after["status"] == "pending"
    assert "預算" in after["note"]
    assert after["attempts"] == 0  # 不消耗 attempts（in_progress 的 +1 已還原）
    assert history.list_sessions()  # 討論 session 已照常落歷史


@pytest.mark.asyncio
async def test_run_one_task_gate_failure_consumes_attempts_control(monkeypatch, tmp_path):
    """對照黑樣本：走 _handle_gate_failure 的 merge 失敗會 attempts+1，證明上測的
    「不消耗」是預算分流的特別行為而非普遍現象。"""
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 0)  # 不限制 → 走到 merge
    _patch_run_one_task_machinery(monkeypatch, tmp_path)
    spy = RunSpy()
    monkeypatch.setattr(autopilot, "_run", spy)

    async def _merge_fail(clone, task):
        return (False, "push 失敗")

    monkeypatch.setattr(autopilot, "_commit_push_merge", _merge_fail)

    task = backlog.add("失敗任務")
    await autopilot.run_one_task(task)

    after = backlog.list_tasks()[0]
    assert after["status"] == "pending"  # 還有重試額度 → 退回 pending
    assert after["attempts"] == 1  # 但 attempts 被消耗
