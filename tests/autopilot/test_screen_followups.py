"""discovered 後續任務進場品質防線（完成率修法 F）。

背景（完成率診斷）：`source="eval"` 的自我發掘走完整 pre-filter（近期完成去重 +
_filter_pending_duplicates 相似度/子系統廣度），但 run_one_task 尾端把討論 followup 直接
add_items/add_many（source="discovered"）繞過所有品質閘 → 收尾驗收/QA 類 no-op 元任務與
重疊提案灌爆 backlog（191 pending 在長）。_screen_followups 是三個 retro emitter 匯流的單一
choke point，套上與 eval 路徑相同的防線。

純檔案 IO + monkeypatch，不打 LLM。
"""

from __future__ import annotations

import pytest

from studio import autopilot, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    return tmp_path


def test_empty_passthrough(state):
    assert autopilot._screen_followups([], ["排隊中"]) == []


def test_drops_recently_done(state, monkeypatch):
    """近期已完成的同名 followup 不該重排（避免做完又重提）。"""
    monkeypatch.setattr(autopilot, "_recent_done_titles", lambda: {"補上 X 的測試"})
    items = [{"title": "補上 X 的測試"}, {"title": "新增 Y 功能"}]
    out = autopilot._screen_followups(items, [])
    assert [i["title"] for i in out] == ["新增 Y 功能"], "已完成的應被去重、其餘保留"


def test_drops_duplicate_of_pending(state, monkeypatch):
    """與排隊任務（pending/in_progress）重複的提案被 pre-filter 擋下。"""
    monkeypatch.setattr(autopilot, "_recent_done_titles", lambda: set())
    existing = ["修復 rotation 死鎖"]
    items = [{"title": "修復 rotation 死鎖"}, {"title": "完全不相干的新任務"}]
    out = autopilot._screen_followups(items, existing)
    titles = [i["title"] for i in out]
    assert "修復 rotation 死鎖" not in titles, "與排隊任務等值/高相似的提案應被擋"
    assert "完全不相干的新任務" in titles, "不相干提案不得誤殺"


def test_structured_items_preserve_fields_and_order(state, monkeypatch):
    """結構化 dict 通過後保留 detail/priority/type 等欄位與原順序。"""
    monkeypatch.setattr(autopilot, "_recent_done_titles", lambda: set())
    items = [
        {"title": "任務甲", "detail": "細節甲", "priority": 0, "type": "bug"},
        {"title": "任務乙", "detail": "細節乙", "priority": 2},
    ]
    out = autopilot._screen_followups(items, [])
    assert out == items, "無重複時全數保留、型別/欄位/順序不變"


def test_plain_title_strings_supported(state, monkeypatch):
    """followups（純標題字串）路徑也走同一防線。"""
    monkeypatch.setattr(autopilot, "_recent_done_titles", lambda: {"已完成標題"})
    out = autopilot._screen_followups(["已完成標題", "全新標題"], [])
    assert out == ["全新標題"]


def test_blank_titles_dropped(state, monkeypatch):
    monkeypatch.setattr(autopilot, "_recent_done_titles", lambda: set())
    out = autopilot._screen_followups([{"title": "   "}, {"title": "有效任務"}], [])
    assert [i["title"] for i in out] == ["有效任務"]
