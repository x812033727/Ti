"""orchestrator 後續任務解析的單元測試（autopilot 回饋迴圈用）。"""

from __future__ import annotations

from studio.orchestrator import parse_followups


def test_parse_followups_extracts_lines():
    text = (
        "這次做得不錯。\n"
        "後續任務: 補上 download 路由的測試\n"
        "後續任務：重構 runner 的逾時處理\n"
        "其他閒聊。\n"
    )
    out = parse_followups(text)
    assert out == ["補上 download 路由的測試", "重構 runner 的逾時處理"]


def test_parse_followups_none():
    assert parse_followups("沒有後續，全部完成。") == []
