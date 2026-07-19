"""QA 獨立驗證任務 #2：flow.parse_agenda / flow.validate_assignees。

與工程師單測（test_agenda.py）互補，聚焦：
1. 同一份 PM 拆解文本同時含 `子題:`/`負責:` 與既有 `任務:`/`依賴:` 行——
   parse_agenda 與 parse_tasks_with_deps 各取所需、互不干擾（零回歸關鍵）。
2. 自證對應：解析輸出的標題/分派必須能逐一回指本次輸入行，排除假綠。
3. 邊界怪輸入：`負責:` 帶多餘文字、行內縮排、Windows 換行、None-ish 空輸入。
"""

from __future__ import annotations

from studio.flow import (
    MAX_AGENDA_ITEMS,
    parse_agenda,
    parse_tasks_with_deps,
    validate_assignees,
)

# 模擬 StubExpert 一次輸出的「疊加格式」拆解文本（架構定案：同一呼叫產出議程＋任務）。
PM_TEXT = (
    "本輪拆解如下：\n"
    "子題: 資料層 | 設計 schema | 遷移可重複執行\n"
    "負責: engineer\n"
    "子題: 服務層 | API 與驗證 | curl 全流程 2xx\n"
    "負責: senior\n"
    "任務: #1 建表與遷移\n"
    "任務: #2 寫 API endpoint\n"
    "依賴: #2 -> #1\n"
)


def test_agenda_and_tasks_coexist_no_interference():
    items = parse_agenda(PM_TEXT, requirement="原始需求")
    tasks, deps = parse_tasks_with_deps(PM_TEXT)
    # 議程只取子題行，不把 任務:/依賴: 行誤吞。
    assert [i["title"] for i in items] == ["資料層", "服務層"]
    assert [i["assignee"] for i in items] == ["engineer", "senior"]
    # 既有任務解析不受 子題:/負責: 行影響（零回歸）。
    assert [t["title"] for t in tasks] == ["建表與遷移", "寫 API endpoint"]
    assert deps == [(2, 1)]


def test_output_traces_back_to_input_lines():
    # 自證對應：每筆輸出標題與 assignee 都必須能在輸入文本中找到對應行。
    items = parse_agenda(PM_TEXT)
    for it in items:
        assert any(
            ln.startswith("子題:") and it["title"] in ln for ln in PM_TEXT.splitlines()
        ), f"標題 {it['title']!r} 無法回指輸入"
        assert f"負責: {it['assignee']}" in PM_TEXT


def test_anti_false_green_black_sample():
    # 反向黑樣本：完全無標記文本不得幻覺出多子題或任何 assignee。
    black = "這是一段沒有任何結構標記的閒聊。\n大家加油。"
    items = parse_agenda(black, requirement="需求X")
    assert len(items) == 1
    assert items[0]["title"] == "需求X"
    assert items[0]["assignee"] == ""


def test_assignee_with_trailing_words_not_matched():
    # `負責: engineer 先生` 不符 `<role_key>` 單一 token 規格——不採信、不噴錯。
    items = parse_agenda("子題: 甲\n負責: engineer 先生\n")
    assert items[0]["assignee"] == ""


def test_indented_lines_and_crlf():
    items = parse_agenda("  子題: 甲 | 乙 | 丙\r\n  負責: engineer\r\n")
    assert items[0] == {
        "title": "甲",
        "description": "乙",
        "criteria": "丙",
        "assignee": "engineer",
    }


def test_empty_and_whitespace_inputs_no_crash():
    assert parse_agenda("", requirement="")[0]["title"] == "實作需求"
    assert parse_agenda("   \n  ")[0]["title"] == "實作需求"


def test_exactly_max_items_no_truncation_warning(caplog):
    text = "\n".join(f"子題: T{i}" for i in range(MAX_AGENDA_ITEMS))
    with caplog.at_level("WARNING"):
        items = parse_agenda(text)
    assert len(items) == MAX_AGENDA_ITEMS
    assert "截斷" not in caplog.text  # 恰好 5 筆不得誤報截斷。


def test_validate_dedup_available_keys_and_dict_input():
    # available_keys 常見來源是 experts dict——傳 keys view 也要可用；重複 key 去重保序。
    experts = {"researcher": object(), "engineer": object()}
    agenda = [{"title": "甲", "description": "", "criteria": "", "assignee": "nope"}]
    out, corr = validate_assignees(agenda, experts.keys())
    assert out[0]["assignee"] == "engineer"
    out2, _ = validate_assignees(agenda, ["senior", "senior", "pm"])
    assert out2[0]["assignee"] == "senior"  # engineer 缺席→第一個出席者（去重保序）。


def test_corrections_only_for_invalid_entries():
    agenda = [
        {"title": "甲", "description": "", "criteria": "", "assignee": "senior"},
        {"title": "乙", "description": "", "criteria": "", "assignee": "ghost"},
        {"title": "丙", "description": "", "criteria": "", "assignee": ""},
    ]
    out, corr = validate_assignees(agenda, ["engineer", "senior"])
    assert [o["assignee"] for o in out] == ["senior", "engineer", "engineer"]
    assert [c["index"] for c in corr] == [1, 2]
