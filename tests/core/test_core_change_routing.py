"""雙軌路由的單元測試（不需 LLM）：一場討論結果如何分流回填兩份 backlog。

關鍵不變式（見 ARCHITECTURE.md「專案 repo 與 Ti 主核心 repo」）：
  - 後續任務 → 專案 backlog（per-project state_dir）。
  - 核心改動 → 核心 backlog（預設 config.AUTOPILOT_STATE_DIR，autopilot 在 drain 的那份）。
  - 兩集合不相交：核心改動絕不落入專案 backlog／PR。
"""

from __future__ import annotations

import pytest

from studio import backlog, config
from studio.improver import drain_result_to_backlogs


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """分開的核心 backlog 目錄與專案 backlog 目錄。"""
    core_dir = tmp_path / "core"
    project_dir = tmp_path / "project"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", core_dir)
    return core_dir, project_dir


def test_core_repo_is_pinned_to_ti():
    # 主核心 repo 固定為 Ti 框架本身（綁定 AUTOPILOT_REPO，預設 x812033727/Ti）。
    assert config.CORE_REPO == config.AUTOPILOT_REPO
    assert config.CORE_REPO == "x812033727/Ti"


def test_routing_splits_followups_and_core_changes(dirs):
    core_dir, project_dir = dirs
    result = {
        "completed": True,
        "followups": ["補專案測試"],
        "followup_items": [{"title": "補專案測試", "priority": 1, "type": "improvement"}],
        "core_changes": [{"title": "改 orchestrator", "priority": 0, "type": "feature"}],
    }

    added, routed = drain_result_to_backlogs(result, project_dir)
    assert added == 1 and routed == 1

    project_titles = {t["title"] for t in backlog.list_tasks(state_dir=project_dir)}
    core_tasks = backlog.list_tasks()  # 省略 state_dir＝核心 backlog（AUTOPILOT_STATE_DIR）
    core_titles = {t["title"] for t in core_tasks}

    # 各歸其位。
    assert project_titles == {"補專案測試"}
    assert core_titles == {"改 orchestrator"}
    # 兩集合不相交——核心改動沒有滲進專案 backlog，反之亦然。
    assert project_titles.isdisjoint(core_titles)
    # 核心項目以 source="core" 標記，供稽核。
    assert all(t["source"] == "core" for t in core_tasks)


def test_routing_no_core_changes_leaves_core_backlog_empty(dirs):
    core_dir, project_dir = dirs
    result = {
        "completed": True,
        "followup_items": [{"title": "只是專案後續", "priority": 1, "type": "improvement"}],
        "core_changes": [],
    }

    added, routed = drain_result_to_backlogs(result, project_dir)
    assert added == 1 and routed == 0
    assert backlog.list_tasks() == []  # 核心 backlog 不被無關專案工作污染
    assert {t["title"] for t in backlog.list_tasks(state_dir=project_dir)} == {"只是專案後續"}


def test_routing_legacy_result_without_core_changes_key(dirs):
    """舊 result（無 core_changes 鍵）不應爆炸——用 .get 安全退回。"""
    core_dir, project_dir = dirs
    result = {"completed": True, "followups": ["舊式後續"]}
    added, routed = drain_result_to_backlogs(result, project_dir)
    assert routed == 0
    assert backlog.list_tasks() == []
