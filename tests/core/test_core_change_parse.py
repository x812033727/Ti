"""核心改動解析器的單元測試（不需 LLM）。

驗證 `核心改動:` 結構化行的抽取——這是「專案 repo vs Ti 主核心 repo 雙軌路由」的偵測點：
專家在討論中判定需改 Ti 核心框架時，以此行表態，由 flow.parse_core_changes 解析後路由到核心 repo。
"""

from __future__ import annotations


def test_parse_core_changes_tags():
    from studio.orchestrator import parse_core_changes

    text = (
        "核心改動: [P0/bug] 修 orchestrator 波次死結\n"
        "核心改動: [feature] runner 加沙箱白名單\n"
        "核心改動: [P2] 美化發佈訊息\n"
        "核心改動: 補核心文件\n"
        "核心改動: [亂寫的標籤] 仍要收下\n"
    )
    items = parse_core_changes(text)
    assert [(t["priority"], t["type"]) for t in items] == [
        (0, "bug"),
        (1, "feature"),
        (2, "improvement"),
        (1, "improvement"),
        (1, "improvement"),
    ]
    assert items[0]["title"] == "修 orchestrator 波次死結"
    assert items[4]["title"] == "仍要收下"


def test_parse_core_changes_full_width_colon():
    from studio.orchestrator import parse_core_changes

    # 全形冒號與前導空白都要吃下（沿用既有解析慣例）。
    items = parse_core_changes("  核心改動：在 publisher 加重試\n")
    assert items == [{"title": "在 publisher 加重試", "priority": 1, "type": "improvement"}]


def test_parse_core_changes_does_not_capture_other_markers():
    """核心改動不可吃到後續任務／教訓／任務行——兩條軌道必須分流，互不污染。"""
    from studio.orchestrator import parse_core_changes

    text = "後續任務: 補專案測試\n教訓: 早點寫測試\n任務: 實作功能\n核心改動: 改 Ti 核心發佈流程\n"
    items = parse_core_changes(text)
    assert [t["title"] for t in items] == ["改 Ti 核心發佈流程"]


def test_parse_core_changes_empty_and_whitespace():
    from studio.orchestrator import parse_core_changes

    assert parse_core_changes("") == []
    assert parse_core_changes("沒有任何標記行\n只是一段檢討文字") == []
    # 空白標題剔除（與其他解析器一致）。
    assert parse_core_changes("核心改動:    ") == []


def test_parse_core_changes_caps_at_ten():
    from studio.orchestrator import parse_core_changes

    text = "\n".join(f"核心改動: 第 {i} 項核心改動" for i in range(20))
    assert len(parse_core_changes(text)) == 10
