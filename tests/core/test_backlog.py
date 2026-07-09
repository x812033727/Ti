"""backlog 持久任務佇列的單元測試（不需 LLM）。"""

from __future__ import annotations

import json

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


def _patch_task(task_id: int, **fields) -> None:
    """直接改 backlog.json 任務欄位（設定 updated_at 以測近窗切片）。"""
    p = backlog._path(None)
    data = json.loads(p.read_text(encoding="utf-8"))
    for t in data["tasks"]:
        if t["id"] == task_id:
            t.update(fields)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_completion_stats_excludes_parked_and_pending(state):
    """完成率分母只納 done+failed；parked（永不清）與 pending（未終局）都排除。"""
    for i in range(3):
        t = backlog.add(f"done-{i}")
        backlog.set_status(t["id"], "done")
    tf = backlog.add("failed-1")
    backlog.set_status(tf["id"], "failed", note="討論未達完成")
    tp = backlog.add("parked-1")
    backlog.set_status(tp["id"], "parked", note="無變更")
    backlog.add("pending-1")  # 未終局

    cs = backlog.completion_stats()
    assert cs["done"] == 3 and cs["failed"] == 1 and cs["total"] == 4, cs
    assert cs["rate"] == 0.75, "3 done /(3 done + 1 failed)，parked/pending 不計入分母"


def test_completion_stats_empty_is_none(state):
    backlog.add("只有 pending")
    cs = backlog.completion_stats()
    assert cs == {"window": 50, "done": 0, "failed": 0, "total": 0, "rate": None}


def test_completion_stats_recent_window_slices_by_updated_at(state):
    """近窗只取 updated_at 最近的 window 筆——舊史不該灌水近況。"""
    # 5 筆終局：舊的 3 筆 failed、近的 2 筆 done（以 updated_at 明確排序）
    ids = []
    for i in range(5):
        t = backlog.add(f"t-{i}")
        backlog.set_status(t["id"], "done" if i >= 3 else "failed", note="n")
        ids.append(t["id"])
    for rank, tid in enumerate(ids):
        _patch_task(tid, updated_at=1000.0 + rank)  # rank 越大越新；最新兩筆(3,4)=done
    cs = backlog.completion_stats(window=2)
    assert cs["total"] == 2 and cs["done"] == 2 and cs["rate"] == 1.0, (
        f"近 2 筆應只含最新的兩筆 done、不被舊 failed 拖低：{cs}"
    )
    # 全窗則含全部 5 筆：2 done / 5
    allw = backlog.completion_stats(window=0)
    assert allw["total"] == 5 and allw["done"] == 2 and allw["rate"] == 0.4


def test_pause_switch(tmp_path, monkeypatch):
    pf = tmp_path / "PAUSED"
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", pf)
    monkeypatch.delenv("TI_AUTOPILOT_PAUSED", raising=False)
    assert config.autopilot_paused() is False
    pf.write_text("x")
    assert config.autopilot_paused() is True
