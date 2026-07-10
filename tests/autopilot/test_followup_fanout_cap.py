"""discovered followup 衍生扇出限制（完成率第二輪修法 ②）。

背景（完成率診斷）：discovered 自我衍生 meta 迴圈失控——一個任務繁殖一堆 followup、followup 再繁殖
followup，指數灌爆 backlog。價值閘（①）擋「沒價值的」，本修法從結構上再加兩道與其互補的上限：
- 扇出寬度：單一任務一場最多回填 `AUTOPILOT_FOLLOWUP_MAX_PER_TASK` 個後續（品質過濾後截斷）。
- 血緣代數：父任務 gen 達 `AUTOPILOT_FOLLOWUP_MAX_GEN` 則其 followup 一律不入場，斷深鏈。
子任務 gen＝父+1，隨 backlog.add 落欄位（只 >0 才落，相容既有 backlog）。

純檔案 IO + monkeypatch，不打 LLM/網路。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_PER_TASK", 3)
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_GEN", 3)
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_VALUE_GATE", True)
    # 隔離：去重防線不介入本測（聚焦扇出/血緣上限本身）
    monkeypatch.setattr(autopilot, "_recent_done_titles", lambda: set())
    return tmp_path


# --- backlog.add gen 欄位 --------------------------------------------------


def test_add_stores_gen_only_when_nonzero(state):
    a = backlog.add("一般任務")  # gen 預設 0
    b = backlog.add("衍生任務", source="discovered", gen=2)
    assert "gen" not in a, "gen=0 不落欄位，保持既有 dict 形狀"
    assert b["gen"] == 2


def test_add_many_items_thread_gen(state):
    backlog.add_many(["甲", "乙"], source="discovered", gen=1)
    backlog.add_items([{"title": "丙"}], source="discovered", gen=2)
    by = {t["title"]: t for t in backlog.list_tasks()}
    assert by["甲"]["gen"] == 1 and by["乙"]["gen"] == 1 and by["丙"]["gen"] == 2


# --- 扇出寬度上限 ----------------------------------------------------------


def test_width_cap_truncates_followups(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_PER_TASK", 2)
    raw = ["實作 A 並補測", "修復 B 並補測", "重構 C 並補測", "新增 D 功能並補測"]
    n = autopilot._add_discovered_followups({"id": 1}, raw, [], structured=False)
    assert n == 2, "品質過濾後截斷到寬度上限"
    assert len(backlog.list_tasks()) == 2


def test_width_cap_zero_means_unlimited(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_PER_TASK", 0)
    raw = [f"實作模組 {c} 並補測" for c in "ABCDE"]
    n = autopilot._add_discovered_followups({"id": 1}, raw, [], structured=False)
    assert n == 5, "0＝不限寬度"


# --- 血緣代數上限 ----------------------------------------------------------


def test_child_followups_carry_gen_parent_plus_one(state):
    """父任務 gen=1 → 其 followup gen=2。"""
    n = autopilot._add_discovered_followups(
        {"id": 9, "gen": 1}, ["實作 X 並補測"], [], structured=False
    )
    assert n == 1
    child = next(t for t in backlog.list_tasks() if t["title"] == "實作 X 並補測")
    assert child["gen"] == 2


def test_gen_cap_drops_all_followups_at_limit(state, monkeypatch):
    """父任務已達血緣代數上限 → 其 followup 一律不入場（斷深鏈）。"""
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_GEN", 3)
    n = autopilot._add_discovered_followups(
        {"id": 9, "gen": 3}, ["實作 X 並補測", "修復 Y 並補測"], [], structured=False
    )
    assert n == 0
    assert backlog.list_tasks() == []


def test_gen_below_cap_still_adds(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_GEN", 3)
    n = autopilot._add_discovered_followups(
        {"id": 9, "gen": 2}, ["實作 X 並補測"], [], structured=False
    )
    assert n == 1, "gen=2 < 上限 3，仍可衍生（產物 gen=3）"


def test_gen_cap_zero_means_unlimited(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_GEN", 0)
    n = autopilot._add_discovered_followups(
        {"id": 9, "gen": 99}, ["實作 X 並補測"], [], structured=False
    )
    assert n == 1, "0＝不限代數"


# --- 與價值閘/去重組合、結構化路徑 ----------------------------------------


def test_value_gate_and_width_compose(state, monkeypatch):
    """價值閘先剔 busywork，寬度上限再對剩餘截斷（兩道疊加）。"""
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_PER_TASK", 2)
    raw = [
        "收尾驗收 QA pass 落檔 sha256",  # 價值閘剔除
        "實作 A 並補測",
        "修復 B 並補測",
        "重構 C 並補測",  # 寬度上限截斷
    ]
    n = autopilot._add_discovered_followups({"id": 1}, raw, [], structured=False)
    titles = {t["title"] for t in backlog.list_tasks()}
    assert n == 2 and titles == {"實作 A 並補測", "修復 B 並補測"}


def test_structured_items_path_carries_gen(state):
    items = [{"title": "實作 A 並補測", "priority": 0, "type": "bug"}]
    autopilot._add_discovered_followups({"id": 1, "gen": 0}, items, [], structured=True)
    t = next(x for x in backlog.list_tasks() if x["title"] == "實作 A 並補測")
    assert t["gen"] == 1 and t["priority"] == 0 and t["type"] == "bug"


def test_empty_raw_noop(state):
    assert autopilot._add_discovered_followups({"id": 1}, [], [], structured=False) == 0
