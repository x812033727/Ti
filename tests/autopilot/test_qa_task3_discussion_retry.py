"""QA 驗收：任務 #3「討論未達完成且不可出貨」改為有限次重試。

逐條覆蓋：
1. 首次不可出貨不再單發 failed，而是 pending + attempts 遞增。
2. 重試額度用罄才 failed。
3. shippable=True 的舊路徑仍續走客觀閘門並完成。
4. 新設定預設 2、可由 env 覆寫，且小於客觀閘門預設 3。
5. note 保留「討論未達完成」且不被 failed triage 當成 infra 失敗。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from studio import autopilot, backlog, config

ROOT = Path(__file__).resolve().parents[2]


def _capture_discussion_decision(monkeypatch, *, cap: int, task: dict) -> tuple[int, str, dict]:
    calls: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", cap)
    monkeypatch.setattr(
        autopilot.backlog,
        "set_status",
        lambda task_id, status, **kw: calls.append((task_id, status, kw)),
    )

    autopilot._handle_discussion_incomplete(task)

    assert len(calls) == 1
    return calls[0]


def test_ac1_first_incomplete_discussion_requeues_instead_of_failing(monkeypatch):
    tid, status, kw = _capture_discussion_decision(
        monkeypatch,
        cap=2,
        task={"id": 31, "title": "未收斂", "attempts": 0},
    )

    assert (tid, status) == (31, "pending")
    assert kw["attempts"] == 1
    assert "討論未達完成" in kw["note"]


def test_ac2_cap_exhausted_marks_failed_and_keeps_diagnostic_note(monkeypatch):
    tid, status, kw = _capture_discussion_decision(
        monkeypatch,
        cap=2,
        task={"id": 32, "title": "仍未收斂", "attempts": 1},
    )

    assert (tid, status) == (32, "failed")
    assert "討論未達完成" in kw["note"]
    assert "連續 2 次" in kw["note"]


def _install_run_one_task_mocks(monkeypatch, tmp_path, result, statuses, gate_calls):
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _requirement):
            return result

    async def fake_gate(*_args, **_kwargs):
        gate_calls.append(True)
        return True, ""

    async def fake_merge(*_args, **_kwargs):
        return True, "merged"

    async def fake_idle():
        return False

    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
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
    monkeypatch.setattr(autopilot.backlog, "add", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot, "_gate_lint", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_tests", fake_gate)
    monkeypatch.setattr(autopilot, "_commit_push_merge", fake_merge)
    monkeypatch.setattr(autopilot, "_wait_until_idle", fake_idle)


@pytest.mark.asyncio
async def test_ac3_shippable_incomplete_still_runs_gates_and_finishes(monkeypatch, tmp_path):
    statuses: list[tuple[int, str, dict]] = []
    gate_calls: list[bool] = []
    result = {
        "completed": False,
        "shippable": True,
        "followups": [],
        "followup_items": [],
        "core_changes": [],
    }
    _install_run_one_task_mocks(monkeypatch, tmp_path, result, statuses, gate_calls)

    await autopilot.run_one_task({"id": 33, "title": "可帶限制出貨"})

    assert len(gate_calls) == 3
    assert statuses[-1][0:2] == (33, "done")
    assert "已知限制" in statuses[-1][2].get("note", "")
    assert not any(status == "failed" for _, status, _ in statuses)


@pytest.mark.asyncio
async def test_ac1_integration_not_shippable_returns_before_gates(monkeypatch, tmp_path):
    statuses: list[tuple[int, str, dict]] = []
    gate_calls: list[bool] = []
    result = {
        "completed": False,
        "shippable": False,
        "followups": [],
        "followup_items": [],
        "core_changes": [],
    }
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 2)
    _install_run_one_task_mocks(monkeypatch, tmp_path, result, statuses, gate_calls)

    await autopilot.run_one_task({"id": 34, "title": "不可出貨"})

    assert statuses[-1][0:2] == (34, "pending")
    assert statuses[-1][2]["attempts"] == 1
    assert not gate_calls
    assert not any(status == "failed" for _, status, _ in statuses)


def _read_config_values(env: dict[str, str]) -> tuple[int, int]:
    code = (
        "from studio import config; "
        "print(config.AUTOPILOT_DISCUSSION_MAX_ATTEMPTS, "
        "config.AUTOPILOT_TASK_MAX_ATTEMPTS)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    values = [int(part) for part in proc.stdout.split()]
    assert len(values) == 2
    return values[0], values[1]


def test_ac4_config_default_env_override_and_example_documented():
    clean_env = dict(os.environ)
    clean_env.pop("TI_AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", None)
    default_discussion_cap, default_gate_cap = _read_config_values(clean_env)

    override_env = dict(clean_env)
    override_env["TI_AUTOPILOT_DISCUSSION_MAX_ATTEMPTS"] = "4"
    overridden_discussion_cap, _ = _read_config_values(override_env)

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    example_line = next(
        line for line in example.splitlines() if "TI_AUTOPILOT_DISCUSSION_MAX_ATTEMPTS" in line
    )

    assert default_discussion_cap == 2
    assert default_gate_cap == 3
    assert default_discussion_cap < default_gate_cap
    assert overridden_discussion_cap == 4
    assert "TI_AUTOPILOT_DISCUSSION_MAX_ATTEMPTS=2" in example_line
    assert "達上限才 failed" in example_line


def test_ac5_discussion_note_stays_non_infra_for_triage(monkeypatch):
    pending = _capture_discussion_decision(
        monkeypatch,
        cap=3,
        task={"id": 35, "title": "先重試", "attempts": 1},
    )
    failed = _capture_discussion_decision(
        monkeypatch,
        cap=3,
        task={"id": 36, "title": "後失敗", "attempts": 2},
    )

    notes = [pending[2]["note"], failed[2]["note"]]
    assert all("討論未達完成" in note for note in notes)
    assert all(backlog.INFRA_FAILURE_RE.search(note) is None for note in notes)
