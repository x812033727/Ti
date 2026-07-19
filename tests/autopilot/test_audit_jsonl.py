"""autopilot/audit.jsonl 結構化審計紀錄守護。

契約：run_one_task 走到 merge（成功或失敗）即 append 一行 JSON，schema 固定含
ts/task_id/pr/branch/head_sha/outcome/detail/duration_s/attempts；成功 outcome=merged、
失敗 outcome=merge_failed（PR 已開者帶 pr 編號，供每日預算計數）；dryrun 不落檔；
audit 寫入失敗只吞掉（絕不弄死主迴圈）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from studio import autopilot, backlog, config

_AUTOPILOT_REPO = "core/autopilot"


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
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 0)


def _patch_machinery(monkeypatch, tmp_path):
    async def _fake_clone():
        return str(tmp_path / "clone")

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def run(self, requirement):
            return {"completed": True}

    async def _gate_ok(clone):
        return (True, "")

    async def _run(cmd, cwd=None, timeout=600, **kwargs):
        if "rev-parse" in cmd:
            return (0, "abc1234\n")
        return (0, "")

    async def _never_idle(timeout=600):
        return False  # 跳過重佈段

    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    monkeypatch.setattr(autopilot, "StudioSession", _FakeSession)
    monkeypatch.setattr(autopilot, "_gate_lint", _gate_ok)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", _gate_ok)
    monkeypatch.setattr(autopilot, "_gate_tests", _gate_ok)
    monkeypatch.setattr(autopilot, "_run", _run)
    monkeypatch.setattr(autopilot, "_wait_until_idle", _never_idle)


def _audit_lines() -> list[dict]:
    path = autopilot._audit_path()
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


_SCHEMA_KEYS = {
    "ts",
    "task_id",
    "source",  # F2b:寫入端內嵌任務 source(自主度量測免疫 backlog 重建)
    "pr",
    "branch",
    "head_sha",
    "outcome",
    "detail",
    "duration_s",
    "attempts",
}


@pytest.mark.asyncio
async def test_merged_writes_one_full_record(monkeypatch, tmp_path):
    _patch_machinery(monkeypatch, tmp_path)

    async def _merge_ok(clone, task):
        return autopilot.MergeResult(
            True, "已合併", pr_number=42, branch=f"autopilot/task-{task['id']}"
        )

    monkeypatch.setattr(autopilot, "_commit_push_merge", _merge_ok)
    task = backlog.add("成功任務")
    await autopilot.run_one_task(task)

    rows = _audit_lines()
    assert len(rows) == 1
    rec = rows[0]
    assert set(rec) == _SCHEMA_KEYS
    assert rec["outcome"] == "merged"
    assert rec["pr"] == 42
    assert rec["branch"] == f"autopilot/task-{task['id']}"
    assert rec["head_sha"] == "abc1234"
    assert rec["task_id"] == task["id"]
    assert rec["ts"] > 0 and rec["duration_s"] >= 0
    assert backlog.list_tasks()[0]["status"] == "done"  # 審計不影響既有出貨路徑


@pytest.mark.asyncio
async def test_merge_failed_with_pr_records_pr_number(monkeypatch, tmp_path):
    """PR 已開但 CI 未過：outcome=merge_failed 且帶 pr（燒了成本要計入每日預算）。"""
    _patch_machinery(monkeypatch, tmp_path)

    async def _merge_fail(clone, task):
        return autopilot.MergeResult(
            False, "CI 未過或合併失敗（blocked）", pr_number=43, branch="autopilot/task-x"
        )

    monkeypatch.setattr(autopilot, "_commit_push_merge", _merge_fail)
    task = backlog.add("CI 紅任務")
    await autopilot.run_one_task(task)

    rows = _audit_lines()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "merge_failed"
    assert rows[0]["pr"] == 43
    assert autopilot._todays_pr_count() == 1  # 失敗但已開 PR → 計入


@pytest.mark.asyncio
async def test_merge_failed_before_pr_records_null_pr(monkeypatch, tmp_path):
    """push 前就被擋（純 tuple、無 PR）：記錄但 pr=None，不計入每日預算。"""
    _patch_machinery(monkeypatch, tmp_path)

    async def _merge_fail(clone, task):
        return (False, "push 失敗")

    monkeypatch.setattr(autopilot, "_commit_push_merge", _merge_fail)
    task = backlog.add("push 失敗任務")
    await autopilot.run_one_task(task)

    rows = _audit_lines()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "merge_failed"
    assert rows[0]["pr"] is None
    assert autopilot._todays_pr_count() == 0  # 沒開到 PR → 不計


@pytest.mark.asyncio
async def test_dryrun_writes_no_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    _patch_machinery(monkeypatch, tmp_path)

    async def _merge_dry(clone, task):
        return (True, "[dryrun] 會 push")

    monkeypatch.setattr(autopilot, "_commit_push_merge", _merge_dry)
    task = backlog.add("dryrun 任務")
    await autopilot.run_one_task(task)

    assert not autopilot._audit_path().exists()


def test_append_audit_failure_swallowed(monkeypatch):
    """審計寫入失敗（如磁碟滿）只吞掉留 log，不得往外拋。"""

    def _boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(autopilot, "secure_write_root", _boom)
    autopilot._append_audit({"ts": 1.0, "pr": 1})  # 不 raise 即通過
    assert autopilot._todays_pr_count() == 0


def test_history_record_event_style_ownership(monkeypatch, tmp_path):
    """首次建檔走 secure_write_root（REQUIRE_CHOWN 範式）、之後 append 不重建。"""
    created: list = []
    real = autopilot.secure_write_root

    def spy(path, data):
        created.append(str(path))
        return real(path, data)

    monkeypatch.setattr(autopilot, "secure_write_root", spy)
    autopilot._append_audit({"ts": 1.0, "pr": 1})
    autopilot._append_audit({"ts": 2.0, "pr": 2})
    assert len(created) == 1  # 只有首次建空檔經 secure_write_root
    assert len(_audit_lines()) == 2
