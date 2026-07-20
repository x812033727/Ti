"""`flow.parse_investigation` / `flow.parse_incomplete_reason` 純解析單測（完成率第三輪修法一）。

調查分流輕量管線的解析層：單專家輸出的 `結論:/證據:/後續任務:/需人工:/需改碼:` 行標記。
純字串、無 LLM/網路；政策判斷（done/parked/升級）在 autopilot 端，見 test_investigation_lane.py。
"""

from __future__ import annotations

from studio import flow


def test_full_output_with_multiline_conclusion_and_markers():
    text = (
        "我查了一下相關程式。\n"
        "結論: 逾時的根因是 watchdog 未涵蓋 fetch 階段，\n"
        "導致慢速網路下卡在 DNS 解析而無人回收。\n"
        "證據: studio/runner.py:120 watchdog 只包 run 階段\n"
        "證據: journalctl 顯示 fetch 卡 300s 無回收\n"
        "後續任務: 把 watchdog 範圍擴到 fetch 階段並補守門測試\n"
    )
    out = flow.parse_investigation(text)
    assert "watchdog 未涵蓋 fetch 階段" in out["conclusion"]
    assert "無人回收" in out["conclusion"], "結論須支援多行（至下一個標記為止）"
    assert "證據:" not in out["conclusion"], "證據行不得混入結論"
    assert out["evidence"] == [
        "studio/runner.py:120 watchdog 只包 run 階段",
        "journalctl 顯示 fetch 卡 300s 無回收",
    ]
    assert out["needs_human"] == "" and out["needs_code"] == ""
    assert [f["title"] for f in out["followups"]] == ["把 watchdog 範圍擴到 fetch 階段並補守門測試"]


def test_needs_human_and_needs_code_markers():
    out = flow.parse_investigation("需人工: 需要到 GitHub 後台換發 token")
    assert out["needs_human"] == "需要到 GitHub 後台換發 token"
    assert out["conclusion"] == ""

    out = flow.parse_investigation("需改碼: 這其實要改 runner 的重試邏輯才算完成")
    assert out["needs_code"] == "這其實要改 runner 的重試邏輯才算完成"
    assert out["conclusion"] == ""


def test_empty_or_unmarked_text_returns_empty():
    for text in ("", "我調查了很多東西但忘了照格式輸出", "任務: 看起來像別的標記"):
        out = flow.parse_investigation(text)
        assert out["conclusion"] == ""
        assert out["evidence"] == []
        assert out["needs_human"] == "" and out["needs_code"] == ""


def test_last_conclusion_wins_and_fullwidth_colon():
    text = "結論: 初步猜測是 A\n重新驗證後——\n結論：確認根因是 B\n證據：config.py:10\n"
    out = flow.parse_investigation(text)
    assert out["conclusion"] == "確認根因是 B", "取最後一個 `結論:`（與 _last_match 慣例一致）"
    assert out["evidence"] == ["config.py:10"]


def test_parse_incomplete_reason():
    assert (
        flow.parse_incomplete_reason("決議: 未完成\n原因: QA 無法存取 $TMPDIR 證據檔")
        == "QA 無法存取 $TMPDIR 證據檔"
    )
    assert flow.parse_incomplete_reason("決議: 完成") == ""
    # 多個取最後
    assert flow.parse_incomplete_reason("原因: 舊\n原因：新的根因") == "新的根因"
