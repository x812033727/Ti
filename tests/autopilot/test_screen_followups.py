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


def test_pending_filter_dedups_semantic_subset_with_long_evidence(state):
    """證據/理由不同不應稀釋主旨：部署預設與 confirm time 的實際重提要被攔。"""
    existing = ["讓部署黑盒驗證在生產預設生效並補實證（config.py:801 TI_DEPLOY_VERIFY 預設 0）"]
    proposals = [
        "讓部署黑盒驗證在生產預設路徑生效（deploy.py:278 依賴 config.py:801），"
        "使 health 綠但 auth 壞的部署能被攔截",
        "新增部署結果的使用者可見時間軸",
    ]
    assert autopilot._filter_pending_duplicates(proposals, existing) == [
        "新增部署結果的使用者可見時間軸"
    ]


def test_pending_filter_dedups_within_same_batch_even_without_existing(state):
    proposals = [
        "修復引導式預約流程遺漏人數選擇",
        "修正引導式預約主流程缺少人數選擇",
        "在可用時段查詢過濾已經過去的時間",
    ]
    assert autopilot._filter_pending_duplicates(proposals, []) == [
        "修復引導式預約流程遺漏人數選擇",
        "在可用時段查詢過濾已經過去的時間",
    ]


def test_subject_overlap_does_not_merge_distinct_fields(state):
    existing = ["修正預約日期確認訊息"]
    assert autopilot._filter_pending_duplicates(["修正預約人數確認訊息"], existing) == [
        "修正預約人數確認訊息"
    ]


def test_subject_overlap_preserves_distinct_ascii_identifiers(state):
    proposals = [
        "實作模組 A 並補測",
        "實作模組 B 並補測",
        "新增付款方式：Apple Pay，目前只支援現金",
        "新增付款方式：Google Pay，目前只支援現金",
        "支援通知（iOS）",
        "支援通知（macOS）",
    ]
    assert autopilot._filter_pending_duplicates(proposals, []) == proposals
