"""open PR reconciler（完成率第三輪修法二B）：merging 任務收斂 + 孤兒 PR 認領。

背景：auto-merge 把「等 CI→合併」交還 GitHub 後，任務多了 merging 懸置態；加上中斷殘留
的孤兒 PR（歷史缺口：全庫原本沒有任何人回頭認領 open PR）。`_reconcile_open_prs` 是兩者
的唯一收斂點，每 900s 由主迴圈在取任務前呼叫。

覆蓋：
- Pass 1（merging）：MERGED→done＋audit 補記（**pr_ref 欄，不帶 pr——防 _todays_pr_count
  雙計每日預算**）；CLOSED→gate failure 退回；BEHIND→publisher._update_branch（strict:true
  必要配套，輪數上限）；CI 紅/DIRTY→關 PR 退回；pending 未逾齡→不動；逾齡→關 PR 退回
  （note 帶「逾時」可分診）。
- Pass 2（孤兒）：pending 任務＋綠 PR→掛 auto-merge 認領改 merging；done/不存在→關閉清理。
- 節流；gh 失敗容錯（壞 JSON/rc!=0 跳過不炸）。

全程攔截 autopilot._run 與 publisher._update_branch，零真實 gh/網路。
"""

from __future__ import annotations

import json

import pytest

from studio import autopilot, backlog, config, publisher


class GhSpy:
    """攔截 autopilot._run：以指令片段分派可控輸出，記錄呼叫序列。"""

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600):
        self.calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        for key, val in self.overrides.items():
            if key in joined:
                return val
        return (0, "")

    def called(self, fragment: str) -> bool:
        return any(fragment in " ".join(str(x) for x in c) for c in self.calls)


def _view_json(state="OPEN", merge_state="BLOCKED", rollup=None):
    return (
        0,
        json.dumps(
            {"state": state, "mergeStateStatus": merge_state, "statusCheckRollup": rollup or []}
        ),
    )


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_AUTO_MERGE", True)
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "o/r")
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_MAX_AGE", 7200)
    monkeypatch.setattr(config, "MERGE_BEHIND_RETRIES", 2)
    monkeypatch.setattr(config, "AUTOPILOT_TASK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(autopilot, "_last_reconcile_at", 0.0)
    # publisher repo override 走 no-op（不打 REST）
    monkeypatch.setattr(publisher, "set_repo_override", lambda repo: object())
    monkeypatch.setattr(publisher, "reset_repo_override", lambda token: None)
    return tmp_path


def _install(monkeypatch, overrides, *, update_branch_ok=True):
    spy = GhSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    updates: list[int] = []

    async def _update(number):
        updates.append(number)
        return update_branch_ok

    monkeypatch.setattr(publisher, "_update_branch", _update)
    audits: list[dict] = []
    monkeypatch.setattr(autopilot, "_append_audit", lambda rec: audits.append(rec))
    return spy, updates, audits


def _merging_task(*, pr=42, armed_ago=60.0, behind_rounds=0, monkeypatch=None):
    import time

    t = backlog.add("背景合併中的任務")
    fields = {
        "pr": pr,
        "merged_branch": f"autopilot/task-{t['id']}",
        "merge_armed_at": time.time() - armed_ago,
    }
    if behind_rounds:
        fields["behind_rounds"] = behind_rounds
    backlog.set_status(t["id"], "merging", **fields)
    return t


def _load(task_id):
    return next(t for t in backlog.list_tasks() if t["id"] == task_id)


# --- Pass 1：merging 任務 ---------------------------------------------------


@pytest.mark.asyncio
async def test_merged_pr_converges_to_done_with_pr_ref_audit(monkeypatch, state):
    t = _merging_task()
    spy, _, audits = _install(monkeypatch, {"pr view 42": _view_json(state="MERGED")})

    await autopilot._maybe_reconcile_open_prs()

    updated = _load(t["id"])
    assert updated["status"] == "done"
    assert audits and audits[-1]["outcome"] == "merged" and audits[-1]["reconciled"] is True
    assert audits[-1]["pr"] is None and audits[-1]["pr_ref"] == 42, (
        "補記須用 pr_ref——pr 欄會被 _todays_pr_count 二次計入每日預算"
    )


@pytest.mark.asyncio
async def test_closed_pr_requeues_via_gate_failure(monkeypatch, state):
    t = _merging_task()
    _install(monkeypatch, {"pr view 42": _view_json(state="CLOSED")})

    await autopilot._maybe_reconcile_open_prs()

    updated = _load(t["id"])
    assert updated["status"] == "pending", "外部關閉＝失敗一次，未達上限應退回重試"
    assert "被外部關閉" in updated["note"]


@pytest.mark.asyncio
async def test_behind_triggers_update_branch_and_stays_merging(monkeypatch, state):
    t = _merging_task()
    spy, updates, _ = _install(monkeypatch, {"pr view 42": _view_json(merge_state="BEHIND")})

    await autopilot._maybe_reconcile_open_prs()

    assert updates == [42], "strict:true 下 BEHIND 必須 update-branch（auto-merge 必要配套）"
    updated = _load(t["id"])
    assert updated["status"] == "merging"
    assert updated["behind_rounds"] == 1
    assert not spy.called("pr close")


@pytest.mark.asyncio
async def test_behind_over_retry_cap_closes_and_requeues(monkeypatch, state):
    t = _merging_task(behind_rounds=2)  # == MERGE_BEHIND_RETRIES
    spy, updates, _ = _install(monkeypatch, {"pr view 42": _view_json(merge_state="BEHIND")})

    await autopilot._maybe_reconcile_open_prs()

    assert not updates, "達輪數上限不得再追"
    assert spy.called("pr close")
    assert _load(t["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_ci_failure_closes_and_requeues(monkeypatch, state):
    t = _merging_task()
    spy, _, _ = _install(
        monkeypatch,
        {
            "pr view 42": _view_json(
                merge_state="BLOCKED", rollup=[{"conclusion": "FAILURE", "name": "test (3.12)"}]
            )
        },
    )

    await autopilot._maybe_reconcile_open_prs()

    assert spy.called("pr close")
    updated = _load(t["id"])
    assert updated["status"] == "pending"
    assert "CI 失敗" in updated["note"]


@pytest.mark.asyncio
async def test_pending_ci_within_age_left_alone(monkeypatch, state):
    t = _merging_task(armed_ago=60.0)
    spy, _, _ = _install(monkeypatch, {"pr view 42": _view_json(merge_state="BLOCKED")})

    await autopilot._maybe_reconcile_open_prs()

    assert _load(t["id"])["status"] == "merging", "CI 還在跑且未逾齡：不動"
    assert not spy.called("pr close")


@pytest.mark.asyncio
async def test_pending_ci_over_age_closes_with_infra_note(monkeypatch, state):
    t = _merging_task(armed_ago=8000.0)  # > MERGE_MAX_AGE
    spy, _, _ = _install(monkeypatch, {"pr view 42": _view_json(merge_state="BLOCKED")})

    await autopilot._maybe_reconcile_open_prs()

    assert spy.called("pr close")
    updated = _load(t["id"])
    assert updated["status"] == "pending"
    assert backlog.INFRA_FAILURE_RE.search(updated["note"]), "逾齡 note 須可被 triage 分診"


# --- Pass 2：孤兒 PR --------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_pr_for_pending_task_is_claimed(monkeypatch, state):
    t = backlog.add("被中斷的任務")
    spy, _, _ = _install(
        monkeypatch,
        {
            "pr list": (
                0,
                json.dumps([{"number": 77, "headRefName": f"autopilot/task-{t['id']}"}]),
            ),
        },
    )

    await autopilot._maybe_reconcile_open_prs()

    assert spy.called("pr merge 77"), "孤兒 PR（任務 pending）應掛 auto-merge 認領"
    updated = _load(t["id"])
    assert updated["status"] == "merging"
    assert updated["pr"] == 77


@pytest.mark.asyncio
async def test_orphan_pr_for_done_task_is_closed(monkeypatch, state):
    t = backlog.add("已完成的任務")
    backlog.set_status(t["id"], "done")
    spy, _, _ = _install(
        monkeypatch,
        {
            "pr list": (
                0,
                json.dumps([{"number": 88, "headRefName": f"autopilot/task-{t['id']}"}]),
            ),
        },
    )

    await autopilot._maybe_reconcile_open_prs()

    assert spy.called("pr close 88")
    assert not spy.called("pr merge 88")


@pytest.mark.asyncio
async def test_non_autopilot_branches_ignored(monkeypatch, state):
    spy, _, _ = _install(
        monkeypatch,
        {"pr list": (0, json.dumps([{"number": 99, "headRefName": "feature/human-branch"}]))},
    )

    await autopilot._maybe_reconcile_open_prs()

    assert not spy.called("pr close 99") and not spy.called("pr merge 99")


# --- 節流 / 容錯 / 旋鈕 ------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_skips_within_interval(monkeypatch, state):
    _merging_task()
    spy, _, _ = _install(monkeypatch, {"pr view 42": _view_json(state="MERGED")})

    await autopilot._maybe_reconcile_open_prs()
    n = len(spy.calls)
    await autopilot._maybe_reconcile_open_prs()
    assert len(spy.calls) == n, "節流間隔內不得重跑"


@pytest.mark.asyncio
async def test_gh_failure_is_tolerated(monkeypatch, state):
    t = _merging_task()
    _install(monkeypatch, {"pr view 42": (1, "api error"), "pr list": (1, "api error")})

    await autopilot._maybe_reconcile_open_prs()  # 不拋即通過

    assert _load(t["id"])["status"] == "merging", "查詢失敗任務原地不動，留待下輪"


@pytest.mark.asyncio
async def test_knob_off_disables_reconciler(monkeypatch, state):
    monkeypatch.setattr(config, "AUTOPILOT_AUTO_MERGE", False)
    _merging_task()
    spy, _, _ = _install(monkeypatch, {"pr view 42": _view_json(state="MERGED")})

    await autopilot._maybe_reconcile_open_prs()

    assert not spy.calls
