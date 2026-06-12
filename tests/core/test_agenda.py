"""議程解析與分派硬驗證的純函式單測（flow.parse_agenda / flow.validate_assignees）。

不碰 async / LLM，驗證架構定案的全部邊界：
- 子題行 `子題: 標題 | 描述 | 成功準則` 以 split("|", 2) 切段，多餘 `|` 歸成功準則。
- `負責:` 附屬於前置子題；無前置子題忽略不噴錯。
- 無 `子題:` 行 fallback 單一子題（原需求全文），探索型不硬拆。
- 子題數硬上限 MAX_AGENDA_ITEMS（5）截斷。
- 分派硬驗證：合法 key 照分派；非法/缺漏 fallback engineer；engineer 缺席取第一個出席者。
"""

from __future__ import annotations

from studio.flow import MAX_AGENDA_ITEMS, parse_agenda, validate_assignees

# --- parse_agenda ------------------------------------------------------------


def test_parse_full_three_segments_with_assignees():
    text = (
        "前言廢話\n"
        "子題: 資料模型 | 設計訂單與庫存表 | schema 可建表且通過遷移\n"
        "負責: engineer\n"
        "子題: API 介面 | 下單與查詢端點 | curl 全流程 2xx\n"
        "負責: senior\n"
    )
    items = parse_agenda(text)
    assert [i["title"] for i in items] == ["資料模型", "API 介面"]
    assert items[0]["description"] == "設計訂單與庫存表"
    assert items[0]["criteria"] == "schema 可建表且通過遷移"
    assert [i["assignee"] for i in items] == ["engineer", "senior"]


def test_parse_missing_segments_default_empty():
    items = parse_agenda("子題: 只有標題\n")
    assert items == [{"title": "只有標題", "description": "", "criteria": "", "assignee": ""}]
    items = parse_agenda("子題: 標題 | 只有描述\n")
    assert items[0]["description"] == "只有描述"
    assert items[0]["criteria"] == ""


def test_parse_extra_pipes_go_into_criteria():
    # split("|", 2)：第三段之後的 `|` 全部保留進成功準則，標題/描述不錯切。
    items = parse_agenda("子題: 標題 | 描述 | 準則A | 準則B | 準則C\n")
    assert items[0]["title"] == "標題"
    assert items[0]["description"] == "描述"
    assert items[0]["criteria"] == "準則A | 準則B | 準則C"


def test_assignee_without_preceding_subtopic_is_ignored():
    items = parse_agenda("負責: engineer\n子題: 甲\n")
    assert items == [{"title": "甲", "description": "", "criteria": "", "assignee": ""}]


def test_last_assignee_line_wins_per_subtopic():
    items = parse_agenda("子題: 甲\n負責: pm\n負責: engineer\n")
    assert items[0]["assignee"] == "engineer"


def test_fallback_single_subtopic_with_requirement_fulltext():
    req = "做一個探索型的技術調研，評估三種方案"
    items = parse_agenda("自由發揮的拆解文字，沒有任何子題行。", requirement=req)
    assert items == [{"title": req, "description": "", "criteria": "", "assignee": ""}]


def test_fallback_uses_text_when_requirement_empty():
    items = parse_agenda("只有這段文字")
    assert items[0]["title"] == "只有這段文字"
    # 全空也不噴錯。
    assert parse_agenda("")[0]["title"] == "實作需求"


def test_truncates_over_max_items_and_drops_their_assignees():
    lines = []
    for i in range(MAX_AGENDA_ITEMS + 3):
        lines.append(f"子題: 第{i}題")
        lines.append("負責: engineer")
    items = parse_agenda("\n".join(lines))
    assert len(items) == MAX_AGENDA_ITEMS
    assert [i["title"] for i in items] == [f"第{i}題" for i in range(MAX_AGENDA_ITEMS)]
    # 被截斷子題的 `負責:` 不可錯位附到第 5 題上。
    assert items[-1]["assignee"] == "engineer"


def test_fullwidth_colon_accepted():
    items = parse_agenda("子題： 甲 | 乙 | 丙\n負責： engineer\n")
    assert items[0]["title"] == "甲"
    assert items[0]["assignee"] == "engineer"


def test_fullwidth_pipe_normalized():
    # LLM 常混用全形管線，須正規化切段而非整行誤入 title。
    items = parse_agenda("子題: 甲｜乙｜丙\n")
    assert items[0] == {"title": "甲", "description": "乙", "criteria": "丙", "assignee": ""}


def test_assignee_with_trailing_text_not_adopted_but_logged(caplog):
    # `負責: engineer (主寫)` 不符單一 token 規格——不採信，但記 warning 不靜默吞行
    # （與 QA 測試 test_qa_task2_agenda_parser 的規格一致；採信交 validate 兜底）。
    with caplog.at_level("WARNING", logger="ti.flow"):
        items = parse_agenda("子題: 甲\n負責: engineer (主寫)\n")
    assert items[0]["assignee"] == ""
    assert "不符單一 token" in caplog.text


def test_empty_title_backfilled_from_description():
    items = parse_agenda("子題: | 描述在這 | 準則在這\n")
    assert items[0]["title"] == "描述在這"
    assert items[0]["description"] == ""
    assert items[0]["criteria"] == "準則在這"


def test_empty_title_backfilled_from_criteria_when_no_description():
    items = parse_agenda("子題: | | 只有準則\n")
    assert items[0]["title"] == "只有準則"
    assert items[0]["criteria"] == ""


def test_all_empty_subtopic_line_skipped():
    # 全段皆空的子題行整行跳過，其後的 `負責:` 不可錯位附到前一個子題。
    items = parse_agenda("子題: 甲\n子題: |\n負責: engineer\n")
    assert [i["title"] for i in items] == ["甲"]
    assert items[0]["assignee"] == ""
    # 只有空殼行時退回單子題 fallback。
    assert parse_agenda("子題: | |", requirement="原需求")[0]["title"] == "原需求"


# --- validate_assignees ------------------------------------------------------


def test_valid_key_kept_no_correction():
    agenda = [{"title": "甲", "description": "", "criteria": "", "assignee": "senior"}]
    out, corrections = validate_assignees(agenda, ["engineer", "senior"])
    assert out[0]["assignee"] == "senior"
    assert corrections == []


def test_invalid_key_falls_back_to_engineer():
    agenda = [{"title": "甲", "description": "", "criteria": "", "assignee": "不存在"}]
    out, corrections = validate_assignees(agenda, ["engineer", "senior"])
    assert out[0]["assignee"] == "engineer"
    assert corrections == [{"index": 0, "given": "不存在", "assigned": "engineer"}]


def test_missing_assignee_falls_back():
    agenda = [{"title": "甲", "description": "", "criteria": "", "assignee": ""}]
    out, corrections = validate_assignees(agenda, ["engineer"])
    assert out[0]["assignee"] == "engineer"
    assert corrections[0]["given"] == ""


def test_engineer_absent_falls_back_to_first_attendee():
    # 自訂角色組合下 engineer 自身就是非法 key——取第一個出席者。
    agenda = [{"title": "甲", "description": "", "criteria": "", "assignee": "ghost"}]
    out, corrections = validate_assignees(agenda, ["researcher", "senior"])
    assert out[0]["assignee"] == "researcher"
    assert corrections == [{"index": 0, "given": "ghost", "assigned": "researcher"}]


def test_empty_available_keys_does_not_crash():
    agenda = [{"title": "甲", "description": "", "criteria": "", "assignee": "engineer"}]
    out, corrections = validate_assignees(agenda, [])
    assert out[0]["assignee"] == ""
    assert corrections == [{"index": 0, "given": "engineer", "assigned": ""}]


def test_input_agenda_not_mutated():
    agenda = [{"title": "甲", "description": "", "criteria": "", "assignee": "bad"}]
    validate_assignees(agenda, ["engineer"])
    assert agenda[0]["assignee"] == "bad"
