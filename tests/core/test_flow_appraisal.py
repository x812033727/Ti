"""flow.parse_appraisals（`考核:` 行解析）的單元測試——正反案與全形容錯。

marker 範式同「派工:」：行前綴、全形冒號容錯、逐行收集；分數非 1–5 整數一律丟棄該行，
絕不讓 LLM 亂給的分數直通長期考核庫。純函式、不需 LLM。
"""

from __future__ import annotations

from studio.flow import parse_appraisals

# === 正案 =============================================================


def test_parse_appraisals_basic_lines():
    text = (
        "檢討：整體不錯。\n"
        "考核: claude 5 穩定高質量\n"
        "考核: codex 3 速度偏慢但可用\n"
        "後續任務: 補測試\n"
    )
    assert parse_appraisals(text) == [
        {"target": "claude", "score": 5, "comment": "穩定高質量"},
        {"target": "codex", "score": 3, "comment": "速度偏慢但可用"},
    ]


def test_parse_appraisals_fullwidth_colon_digits_and_score_suffix():
    """全形冒號／全形數字／「分」字尾容錯；target 正規化小寫。"""
    text = "考核：Claude ４分 表現不錯\n考核: engineer 4分 按時交付"
    assert parse_appraisals(text) == [
        {"target": "claude", "score": 4, "comment": "表現不錯"},
        {"target": "engineer", "score": 4, "comment": "按時交付"},
    ]


def test_parse_appraisals_comment_optional():
    assert parse_appraisals("考核: claude 4") == [{"target": "claude", "score": 4, "comment": ""}]


def test_parse_appraisals_indented_line_collected():
    assert parse_appraisals("  考核: minimax 2 常需返工") == [
        {"target": "minimax", "score": 2, "comment": "常需返工"}
    ]


# === 反案（丟棄整行） ==================================================


def test_parse_appraisals_drops_out_of_range_and_non_integer_scores():
    text = (
        "考核: claude 0 太差\n"
        "考核: codex 6 超標\n"
        "考核: minimax 4.5 半分不收\n"
        "考核: antigravity 10 兩位數\n"
        "考核: gemini abc 非數字\n"
    )
    assert parse_appraisals(text) == []


def test_parse_appraisals_ignores_unrelated_and_empty_text():
    assert parse_appraisals("") == []
    assert parse_appraisals(None) == []
    assert parse_appraisals("這次沒有考核\n派工: #1 codex\n教訓: 要寫測試") == []


def test_parse_appraisals_requires_score_token():
    # 缺分數（只有 target 與評語文字開頭非數字）→ 整行丟棄。
    assert parse_appraisals("考核: claude 表現很好") == []
