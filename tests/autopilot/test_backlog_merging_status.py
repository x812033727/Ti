"""backlog 新狀態 `merging`（完成率第三輪修法二B）。

merging＝PR 已開、auto-merge 已掛上、等 CI 由 GitHub 背景合併的「非終局懸置態」。
守護不變量：VALID_STATUS 收錄、set_status 可寫、_is_duplicate 視為在途（防重複進場）、
counts 涵蓋、completion_stats 排除（非終局）、next_pending 不揀、
_recover_stale_in_progress 不誤傷（只掃 in_progress）。
"""

from __future__ import annotations

import pytest

from studio import backlog, config


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    return tmp_path


def test_valid_status_includes_merging_and_set_status_works():
    assert "merging" in backlog.VALID_STATUS
    t = backlog.add("等待背景合併的任務")
    updated = backlog.set_status(t["id"], "merging", pr=42, merged_branch="autopilot/task-1")
    assert updated["status"] == "merging"
    assert updated["pr"] == 42


def test_merging_counts_as_duplicate_and_in_counts():
    t = backlog.add("同名任務")
    backlog.set_status(t["id"], "merging")
    assert backlog.add("同名任務") is None, "merging 在途任務須擋同名重複進場"
    assert backlog.counts()["merging"] == 1


def test_merging_excluded_from_completion_stats_and_next_pending():
    t1 = backlog.add("背景合併中")
    backlog.set_status(t1["id"], "merging")
    t2 = backlog.add("已完成")
    backlog.set_status(t2["id"], "done")

    stats = backlog.completion_stats()
    assert stats["total"] == 1, f"merging 非終局，不得進完成率分母：{stats}"
    assert stats["done"] == 1
    assert backlog.next_pending() is None, "merging 不得被當 pending 揀走"


def test_recover_stale_in_progress_leaves_merging_alone(monkeypatch):
    from studio import autopilot

    t = backlog.add("背景合併中")
    backlog.set_status(t["id"], "merging")
    monkeypatch.setattr(autopilot.history, "busy_sessions", lambda *_a, **_k: [])
    autopilot._recover_stale_in_progress()
    assert backlog.list_tasks("merging")[0]["id"] == t["id"], "merging 不得被 stale 回收誤傷"
