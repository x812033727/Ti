"""單元測試：閘門失敗的「同任務有限重試」收斂處置（_handle_gate_failure）。

設計決策（2026-06-21）：客觀閘門（lint/collect/test/merge）失敗時，舊行為是每次都
`backlog.add("修復X失敗…")` spawn 一個措辭近似的新任務，導致 backlog 無限暴增（自我餵食）。
改為：同一任務退回 pending 重試，最多 AUTOPILOT_TASK_MAX_ATTEMPTS 次，達上限才標 failed，
全程不新增任何「修復X」任務。

驗證（皆以真實 backlog + tmp state_dir，確認任務總數不因閘門失敗而增加）：
1. attempts 未達上限：退回 pending、attempts +1、不新增任何任務。
2. attempts 達上限：標 failed、不新增任何任務。
3. 四個閘門（lint/collect/test/merge）都走同一條 helper（gate_label 透傳到 note）。
4. helper 絕不呼叫 backlog.add（不再 spawn「修復X」），故任務總數恆定。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """把 backlog 預設 state_dir 指向 tmp，讓 _handle_gate_failure 的預設操作落在此處。"""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", d)
    monkeypatch.setattr(config, "AUTOPILOT_TASK_MAX_ATTEMPTS", 3)
    return d


def test_requeues_to_pending_below_limit(state_dir):
    task = backlog.add("實作某功能", state_dir=state_dir)
    before = len(backlog.list_tasks(state_dir=state_dir))

    # 模擬「已被取走跑過一次」的快照：attempts=0（首次閘門失敗）。
    autopilot._handle_gate_failure({"id": task["id"], "attempts": 0}, "lint", "ruff 失敗細節")

    rows = backlog.list_tasks(state_dir=state_dir)
    assert len(rows) == before, "重試不該新增任務"
    t = rows[0]
    assert t["status"] == "pending", "未達上限應退回 pending 重試"
    assert t["attempts"] == 1
    assert "lint" in t.get("note", "")


def test_marks_failed_at_limit(state_dir):
    task = backlog.add("實作某功能", state_dir=state_dir)
    before = len(backlog.list_tasks(state_dir=state_dir))

    # 快照 attempts=2 → +1=3 已達上限（不再 < 3），應標 failed。
    autopilot._handle_gate_failure({"id": task["id"], "attempts": 2}, "test", "pytest 紅")

    rows = backlog.list_tasks(state_dir=state_dir)
    assert len(rows) == before, "達上限放棄也不該新增任務"
    assert rows[0]["status"] == "failed"
    assert "test" in rows[0].get("note", "")


def test_full_retry_cycle_then_failed(state_dir):
    """完整生命週期：連續失敗應在第 MAX 次轉 failed，且任務總數恆為 1（無暴增）。"""
    task = backlog.add("實作某功能", state_dir=state_dir)
    tid = task["id"]

    # 第 1、2 次失敗 → pending 重試
    for snap_attempts in (0, 1):
        autopilot._handle_gate_failure({"id": tid, "attempts": snap_attempts}, "collect", "x")
        rows = backlog.list_tasks(state_dir=state_dir)
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"

    # 第 3 次（快照 attempts=2 → +1=3）達上限 → failed
    autopilot._handle_gate_failure({"id": tid, "attempts": 2}, "collect", "x")
    rows = backlog.list_tasks(state_dir=state_dir)
    assert len(rows) == 1, "整個重試生命週期任務總數恆為 1"
    assert rows[0]["status"] == "failed"


@pytest.mark.parametrize("gate_label", ["lint", "collect", "test", "merge"])
def test_all_gate_labels_share_helper(state_dir, gate_label):
    """四個閘門都走同一條 helper，label 須出現在 note。"""
    task = backlog.add(f"任務-{gate_label}", state_dir=state_dir)
    autopilot._handle_gate_failure({"id": task["id"], "attempts": 0}, gate_label, "細節")
    rows = backlog.list_tasks(state_dir=state_dir)
    assert len(rows) == 1
    assert gate_label in rows[0].get("note", "")
    assert rows[0]["status"] == "pending"


def test_handler_never_spawns_fixup_task(state_dir, monkeypatch):
    """保險絲：helper 絕不呼叫 backlog.add（不再 spawn『修復X』新任務）。"""
    task = backlog.add("實作某功能", state_dir=state_dir)
    spawned = []
    monkeypatch.setattr(autopilot.backlog, "add", lambda *a, **k: spawned.append((a, k)))

    # 未達上限與達上限兩種路徑都不該 spawn。
    autopilot._handle_gate_failure({"id": task["id"], "attempts": 0}, "lint", "x")
    autopilot._handle_gate_failure({"id": task["id"], "attempts": 2}, "test", "x")

    assert spawned == [], "閘門失敗不該再 spawn 修復X 任務"


def test_run_one_task_gate_branches_use_handler():
    """source-level：run_one_task 四個閘門失敗分支都呼叫 _handle_gate_failure。

    （重佈失敗路徑仍可走自己的 backlog.add 修復 regression，不在此 helper 的職責範圍。）
    """
    import ast
    import inspect

    src = inspect.getsource(autopilot.run_one_task)
    tree = ast.parse(src)
    handler_calls = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_handle_gate_failure"
    )
    # lint / collect / test / merge 四個閘門
    assert handler_calls >= 4, f"四個閘門應都走 helper，實得 {handler_calls}"
