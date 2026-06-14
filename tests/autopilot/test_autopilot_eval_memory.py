"""自我評估記憶（self-reinforcing）的單元測試：把迴圈自身近期成敗回饋進評估。

純檔案 IO、不需 LLM/網路；state dir 指向 tmp，直接驗證 backlog 衍生的記憶文字與去重過濾。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 20)
    return tmp_path


def _seed(title: str, status: str, **fields):
    t = backlog.add(title)
    backlog.set_status(t["id"], status, **fields)
    return t


def test_empty_backlog_returns_blank(state):
    assert autopilot._recent_outcomes_context() == ""


def test_includes_done_and_failed_with_note(state):
    _seed("加上 X 測試", "done")
    _seed("重構 Y 模組", "failed", note="測試未通過")
    ctx = autopilot._recent_outcomes_context()
    assert "加上 X 測試" in ctx
    assert "重構 Y 模組" in ctx
    assert "測試未通過" in ctx
    # done 在「勿重複」段、failed 在「勿重蹈」段
    done_idx = ctx.index("已完成")
    failed_idx = ctx.index("失敗")
    assert ctx.index("加上 X 測試") > done_idx
    assert ctx.index("重構 Y 模組") > failed_idx


def test_recent_first_ordering(state):
    older = _seed("舊任務", "done")
    newer = _seed("新任務", "done")
    # 確保 updated_at 嚴格遞增（避免同秒）
    backlog.set_status(older["id"], "done", updated_at=100.0)
    backlog.set_status(newer["id"], "done", updated_at=200.0)
    ctx = autopilot._recent_outcomes_context()
    assert ctx.index("新任務") < ctx.index("舊任務")


def test_respects_limit(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 2)
    for i in range(5):
        t = _seed(f"完成 {i}", "done")
        backlog.set_status(t["id"], "done", updated_at=float(i))
    ctx = autopilot._recent_outcomes_context()
    # 只保留最新 2 筆（完成 4、完成 3）
    assert "完成 4" in ctx and "完成 3" in ctx
    assert "完成 0" not in ctx and "完成 1" not in ctx and "完成 2" not in ctx


def test_zero_disables(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 0)
    _seed("做過的事", "done")
    assert autopilot._recent_outcomes_context() == ""
    assert autopilot._recent_done_titles() == set()


def test_recent_done_titles_for_filter(state):
    _seed("已完成的改善", "done")
    _seed("失敗的嘗試", "failed", note="x")
    titles = autopilot._recent_done_titles()
    assert "已完成的改善" in titles
    # failed 不在 done 過濾集合（失敗的做法允許帶新做法重提，由提示詞引導，不在此硬擋）
    assert "失敗的嘗試" not in titles


# --- 新增測試：pending-awareness 與進場 pre-filter（任務 #4）-------------------


def test_pending_awareness_context_includes_pending_and_in_progress(state):
    """_pending_awareness_context() 須包含所有 pending 與 in_progress 標題。"""
    t1 = backlog.add("修復登入漏洞")
    t2 = backlog.add("新增搜尋功能")
    t3 = backlog.add("優化資料庫查詢")
    backlog.set_status(t3["id"], "in_progress")
    # done/failed 不應出現
    _seed("已完成任務不入清單", "done")
    ctx = autopilot._pending_awareness_context()
    assert "修復登入漏洞" in ctx
    assert "新增搜尋功能" in ctx
    assert "優化資料庫查詢" in ctx
    assert "已完成任務不入清單" not in ctx


def test_pending_awareness_context_empty_when_no_pending(state):
    """無 pending/in_progress 時回空字串。"""
    _seed("已完成的事", "done")
    assert autopilot._pending_awareness_context() == ""


def test_build_discovery_prompt_contains_pending_titles_and_directives(state):
    """_build_discovery_prompt() 輸出須含 pending 標題與兩條硬指令關鍵字。

    驗收標準 1：
    - 含每筆 pending 標題
    - 含「不得提出與現有 pending 實質重疊」（措辭近似）
    - 含「優先廣度」（措辭近似）
    """
    t1 = backlog.add("重構 config 模組")
    t2 = backlog.add("補強 backlog 測試")
    prompt = autopilot._build_discovery_prompt()
    # 標題出現在 prompt 中
    assert "重構 config 模組" in prompt
    assert "補強 backlog 測試" in prompt
    # 兩條硬指令：禁止實質重疊（指令 1）
    assert "實質重疊" in prompt
    # 兩條硬指令：優先廣度（指令 2）
    assert "廣度" in prompt


def test_build_discovery_prompt_directives_present_when_no_pending(state):
    """無 pending 任務時，_build_discovery_prompt() 仍含廣度指令（但措辭不掛空清單）。"""
    prompt = autopilot._build_discovery_prompt()
    # 廣度指令永遠在
    assert "廣度" in prompt
    # 空 pending 時「上列」不應出現（避免措辭懸空）
    assert "上列" not in prompt


def test_filter_pending_duplicates_high_overlap_filtered(state):
    """高重疊提案（ratio ≥ AUTOPILOT_DEDUP_RATIO）應被丟棄，進場數為 0。

    驗收標準 2。
    """
    existing = ["修復登入漏洞"]
    # 幾乎相同的提案
    proposals = [
        "修復登入漏洞",           # 完全相同
        "修復 登入 漏洞",          # 僅多空白
        "修復登入的漏洞",          # 極近似
    ]
    result = autopilot._filter_pending_duplicates(proposals, existing)
    # 所有高重疊提案應被過濾，進場數為 0
    assert len(result) == 0


def test_filter_pending_duplicates_keeps_non_overlapping(state):
    """低重疊提案應通過過濾（不誤殺），進場數不為 0。

    驗收標準 2（黑樣本對照）、驗收標準 3（不刪既有任務）。
    """
    existing = ["修復登入漏洞"]
    unrelated = [
        "新增搜尋功能",
        "優化資料庫查詢速度",
        "補強 backlog 測試覆蓋率",
    ]
    result = autopilot._filter_pending_duplicates(unrelated, existing)
    # 完全不相關的提案全數保留
    assert result == unrelated


def test_filter_pending_duplicates_empty_existing(state):
    """existing_titles 為空時，所有提案直接通過（無多餘遍歷）。"""
    proposals = ["任意提案 A", "任意提案 B"]
    assert autopilot._filter_pending_duplicates(proposals, []) == proposals


def test_filter_does_not_modify_backlog_or_existing_tasks(state):
    """pre-filter 不修改 backlog、不刪既有任務。

    驗收標準 3：僅作用於提案進場前，不改動 backlog 的 _is_duplicate 去重契約。
    """
    # 建立既有 pending 任務
    t1 = backlog.add("修復登入漏洞")
    before_count = len(backlog.list_tasks())
    # 過濾（無論丟棄幾個），backlog 不受影響
    autopilot._filter_pending_duplicates(["修復登入漏洞"], ["修復登入漏洞"])
    after_count = len(backlog.list_tasks())
    assert after_count == before_count
    # 既有任務狀態不變
    tasks = backlog.list_tasks()
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["title"] == "修復登入漏洞"


def test_add_many_unchanged_dedup_contract(state):
    """backlog._is_duplicate 去重契約（字串等值 pending/in_progress）未受 pre-filter 影響。

    驗收標準 3：_is_duplicate 只管完成去重，pre-filter 不動其契約。
    """
    backlog.add("修復登入漏洞")
    # 同標題再加入應被 _is_duplicate 攔住（回傳 None），與 pre-filter 無關
    result = backlog.add("修復登入漏洞")
    assert result is None
    # 只有一筆
    assert len(backlog.list_tasks("pending")) == 1
