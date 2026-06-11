"""backlog 持久任務佇列的單元測試（不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    return tmp_path


def test_add_and_counts(state):
    t = backlog.add("任務 A")
    assert t and t["status"] == "pending" and t["id"] == 1
    backlog.add_many(["任務 B", "任務 C"])
    assert backlog.counts()["pending"] == 3


def test_dedup_pending(state):
    backlog.add("重複任務")
    assert backlog.add("重複任務") is None  # 仍 pending → 視為重複
    assert backlog.counts()["pending"] == 1


def test_empty_title_rejected(state):
    assert backlog.add("   ") is None


def test_next_pending_is_oldest(state):
    a = backlog.add("先")
    backlog.add("後")
    assert backlog.next_pending()["id"] == a["id"]


def test_priority_fields_defaults(state):
    t = backlog.add("預設欄位")
    assert t["priority"] == 1 and t["type"] == "improvement" and t["effort"] == ""


def test_priority_clamp_and_type_norm(state):
    t = backlog.add("夾值", priority=9, item_type="WEIRD")
    assert t["priority"] == 2 and t["type"] == "improvement"
    t2 = backlog.add("負值", priority=-3, item_type="Bug")
    assert t2["priority"] == 0 and t2["type"] == "bug"


def test_next_pending_priority_order(state):
    backlog.add("普通", priority=1)
    p0 = backlog.add("緊急", priority=0)
    backlog.add("加分", priority=2)
    assert backlog.next_pending()["id"] == p0["id"]  # P0 先於更早建立的 P1


def test_next_pending_legacy_items_without_priority(state):
    # 舊格式 JSON（無 priority 欄位）讀回時視為 P1，順序與 FIFO 相同。
    a = backlog.add("舊任務A")
    backlog.add("舊任務B")
    p = backlog._path(None)
    data = backlog._load(None)
    for t in data["tasks"]:
        t.pop("priority", None)
        t.pop("type", None)
        t.pop("effort", None)
    p.write_text(__import__("json").dumps(data), encoding="utf-8")
    assert backlog.next_pending()["id"] == a["id"]


def test_add_items_structured(state):
    n = backlog.add_items(
        [
            {"title": "功能X", "priority": 0, "type": "feature", "detail": "說明"},
            {"title": "功能X", "priority": 0, "type": "feature"},  # 重複標題 → 去重
            {"title": "  "},  # 空標題 → 拒收
            {"title": "修Y", "type": "bug"},
        ],
        source="blueprint",
    )
    assert n == 2
    first = backlog.next_pending()
    assert first["title"] == "功能X" and first["source"] == "blueprint"
    assert first["priority"] == 0 and first["type"] == "feature" and first["detail"] == "說明"


def test_status_transitions(state):
    t = backlog.add("做這個")
    backlog.set_status(t["id"], "in_progress", session_id="s1")
    cur = backlog.list_tasks("in_progress")[0]
    assert cur["attempts"] == 1 and cur["session_id"] == "s1"
    backlog.set_status(t["id"], "done")
    assert backlog.counts()["done"] == 1
    assert backlog.next_pending() is None


def test_invalid_status_raises(state):
    t = backlog.add("x")
    with pytest.raises(ValueError):
        backlog.set_status(t["id"], "bogus")


def test_pause_switch(tmp_path, monkeypatch):
    pf = tmp_path / "PAUSED"
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", pf)
    monkeypatch.delenv("TI_AUTOPILOT_PAUSED", raising=False)
    assert config.autopilot_paused() is False
    pf.write_text("x")
    assert config.autopilot_paused() is True
