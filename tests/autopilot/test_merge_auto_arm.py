"""`_commit_push_merge` 掛 GitHub 原生 auto-merge（完成率第三輪修法二B）。

覆蓋契約：
1. 掛 auto 成功＋快窗內 merged → (True, ...)，不進 `publisher._merge_flow`、不關 PR。
2. 掛 auto 成功＋快窗滿仍 pending → (False, ...) 且 `auto_merge_pending=True`、**不關 PR**
   （成品留在遠端由 GitHub 背景合併，reconciler 收尾）。
3. 掛 auto 失敗 → 完整回退既有同步等 CI 路徑（`_merge_flow` 被呼叫，行為不變）。
4. 旋鈕 0 → 完全舊路徑（不打 `pr merge --auto`）。

手法沿用 test_autopilot_push_merge_flags.py：攔截 autopilot._run、monkeypatch
publisher._merge_flow / _get_pr_status；零真實 git/網路。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config, publisher

_TASK = {"id": "7", "title": "示範任務", "detail": "細節"}
_BRANCH = "autopilot/task-7"

_HAS_CHANGE = {"rev-list --count": (0, "1"), "--json number": (0, "42")}


class RunSpy:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for key, val in self.overrides.items():
            if key in joined:
                return val
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{config.AUTOPILOT_REPO}.git")
        return (0, "")

    def called(self, fragment: str) -> bool:
        return any(fragment in " ".join(c) for c in self.calls)


class MergeFlowSpy:
    def __init__(self):
        self.calls: list = []

    async def __call__(self, number, payload, **kwargs):
        self.calls.append(number)
        return (publisher.MergeOutcome.MERGED, "sha")


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_AUTO_MERGE", True)
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_FAST_WAIT", 1)
    monkeypatch.setattr(config, "PUBLISH_CI_INTERVAL", 0.01)


def _install(monkeypatch, overrides, *, pr_status):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    flow = MergeFlowSpy()
    monkeypatch.setattr(publisher, "_merge_flow", flow)

    async def _status(number, **kwargs):
        return pr_status

    monkeypatch.setattr(publisher, "_get_pr_status", _status)
    return spy, flow


@pytest.mark.asyncio
async def test_armed_and_merged_within_fast_window(monkeypatch):
    spy, flow = _install(monkeypatch, {**_HAS_CHANGE}, pr_status={"merged": True})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True
    assert spy.called("pr merge 42") and spy.called("--auto") and spy.called("--squash")
    assert not flow.calls, "auto-merge 路徑不得再進同步 _merge_flow"
    assert not spy.called("pr close")


@pytest.mark.asyncio
async def test_window_elapsed_leaves_pr_open_with_pending_flag(monkeypatch):
    spy, flow = _install(monkeypatch, {**_HAS_CHANGE}, pr_status={"merged": False})
    res = await autopilot._commit_push_merge("/clone", _TASK)
    ok, msg = res

    assert ok is False
    assert getattr(res, "auto_merge_pending", False) is True
    assert getattr(res, "pr_number", None) == 42
    assert not spy.called("pr close"), "快窗滿不得關 PR（成品留給 GitHub 背景合併）"
    assert not flow.calls
    assert "auto-merge" in msg


@pytest.mark.asyncio
async def test_arm_failure_falls_back_to_sync_flow(monkeypatch):
    spy, flow = _install(
        monkeypatch,
        {**_HAS_CHANGE, "pr merge 42": (1, "Pull request is in clean status")},
        pr_status={"merged": False},
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, "掛失敗須回退同步路徑（本例 _merge_flow 回 MERGED）"
    assert flow.calls == [42], "同步 _merge_flow 須被呼叫一次"


@pytest.mark.asyncio
async def test_knob_off_uses_legacy_path_only(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_AUTO_MERGE", False)
    spy, flow = _install(monkeypatch, {**_HAS_CHANGE}, pr_status={"merged": False})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True
    assert not spy.called("--auto"), "旋鈕關閉不得打 gh pr merge --auto"
    assert flow.calls == [42]


# --- run_one_task 消費：auto_merge_pending → merging ＋ audit merge_pending ---


@pytest.mark.asyncio
async def test_run_one_task_marks_merging_on_auto_merge_pending(monkeypatch, tmp_path):
    """快窗滿：任務標 merging（帶 pr/merged_branch/merge_armed_at）、audit 記 merge_pending
    （pr 欄照帶＝計每日預算）、不走 _handle_gate_failure、不 redeploy。"""
    from studio.autopilot import MergeResult

    statuses: list = []
    audits: list = []
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _req):
            return {
                "completed": True,
                "shippable": True,
                "followups": [],
                "followup_items": [],
                "core_changes": [],
            }

    async def fake_gate(*_a, **_k):
        return (True, "")

    async def fake_merge(*_a, **_k):
        return MergeResult(
            False,
            "auto-merge 已掛上 PR #42，CI 未於快窗內收斂",
            pr_number=42,
            branch="autopilot/task-9",
            auto_merge_pending=True,
        )

    async def fake_idle():
        raise AssertionError("merge_pending 不得走 redeploy 路徑")

    gate_failures: list = []
    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", FakeSession)
    monkeypatch.setattr(autopilot.history, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "finish_session", lambda *a, **k: None)
    monkeypatch.setattr(
        autopilot.backlog,
        "set_status",
        lambda task_id, status, **kw: statuses.append((task_id, status, kw)),
    )
    monkeypatch.setattr(autopilot.backlog, "add_items", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add_many", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot, "_gate_lint", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_tests", fake_gate)
    monkeypatch.setattr(autopilot, "_commit_push_merge", fake_merge)
    monkeypatch.setattr(autopilot, "_wait_until_idle", fake_idle)
    monkeypatch.setattr(autopilot, "_append_audit", lambda rec: audits.append(rec))
    monkeypatch.setattr(autopilot, "_handle_gate_failure", lambda *a, **k: gate_failures.append(a))

    async def fake_run(cmd, cwd=None, timeout=600):
        return (0, "abc123\n")

    monkeypatch.setattr(autopilot, "_run", fake_run)
    monkeypatch.setattr(autopilot.config, "AUTOPILOT_DRYRUN", False)

    await autopilot.run_one_task({"id": 9, "title": "測試任務", "detail": ""})

    final = statuses[-1]
    assert final[1] == "merging"
    assert final[2]["pr"] == 42 and final[2]["merged_branch"] == "autopilot/task-9"
    assert final[2]["merge_armed_at"] > 0
    assert audits and audits[-1]["outcome"] == "merge_pending"
    assert audits[-1]["pr"] == 42, "PR 確實開了，須計入每日預算"
    assert not gate_failures, "merge_pending 不是失敗，不得燒 attempts"
