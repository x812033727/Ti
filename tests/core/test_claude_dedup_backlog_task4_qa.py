"""Task #4 QA：確認 Claude path per-speak 去重缺口已有核心 backlog 票追蹤。

驗收標準（本任務）：
- Claude path 去重缺口有核心 backlog 票追蹤；本場不改 Claude path。

QA 立場（破壞性思考）：不接受「口頭已知限制」當作追蹤。要證明三件事：
  1) 缺口已以「持久、可稽核」的形式被記錄（DECISIONS.md + adr.json）。
  2) 記錄內容是正確缺口（Claude + per-speak/去重 + retry），不是占位字。
  3) 該 `核心改動:` 文字走「真正的生產路由路徑」(parse_core_changes →
     add_items(source="core")) 能落成一張 source=core 的核心 backlog 票，
     且不會被誤路由到專案 backlog。

同時鎖定一個已知限制黑樣本：committed 記錄是「反引號包住、非行首」格式，
parse_core_changes 對它回 []（不可就地再解析路由）。把它測起來，避免後人
誤以為「重解析 DECISIONS.md 就能重新路由」。
"""

import json
from pathlib import Path

import pytest

from studio import backlog, config, flow

ROOT = Path(__file__).resolve().parents[2]

# 缺口的標準（行首）描述——專家在結論輸出時應採用的可路由形式。
CANONICAL_CORE_LINE = (
    "核心改動: Claude provider 路徑缺乏 per-speak 去重保護，retry 時寫入型工具仍可重跑"
)
# committed 記錄裡的形式（核心改動被反引號包住、且不在行首）。
RECORDED_INLINE_FORM = (
    "## Claude provider 路徑的 retry gap 以 `核心改動: Claude provider 路徑缺乏 "
    "per-speak 去重保護，retry 時寫入型工具仍可重跑` 記入 backlog，本次不動 Claude path"
)


@pytest.fixture
def core_state(tmp_path, monkeypatch):
    """把核心 backlog 指向隔離 tmp 目錄，避免污染真實 state，並關閉 done 過濾。"""
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    # route_core_changes 會用 recent_done_titles 過濾；測試環境設 0=不過濾。
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 0)
    return tmp_path


# --- 1) 缺口已被持久記錄（DECISIONS.md） -----------------------------------


def test_gap_recorded_in_decisions_md():
    text = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    assert (
        "核心改動: Claude provider 路徑缺乏 per-speak 去重保護" in text
    ), "DECISIONS.md 未記錄 Claude path per-speak 去重缺口的核心改動票"


# --- 2) 缺口已被結構化記錄（adr.json，且可機器查詢） ----------------------


def test_gap_recorded_in_adr_json_structured():
    data = json.loads((ROOT / "adr.json").read_text(encoding="utf-8"))
    # adr.json 頂層為 {"entries": [...]}（每筆含 decision/rationale/rejected）。
    decisions = data["entries"] if isinstance(data, dict) else data
    hits = [
        d
        for d in decisions
        if isinstance(d, dict)
        and "Claude" in d.get("decision", "")
        and "per-speak" in d.get("decision", "")
        and ("去重" in d.get("decision", "") or "dedup" in d.get("decision", "").lower())
    ]
    assert hits, "adr.json 無 Claude per-speak 去重缺口的結構化決策條目"
    # 該票必須含 retry 語境，否則描述不足以讓接手人定位缺口。
    assert any(
        "retry" in d["decision"] for d in hits
    ), "adr.json 條目未說明 retry 重放情境，描述不足"
    # 必須含「記入 backlog／不動 Claude path」的範圍宣告（本場不實作）。
    assert any(
        "backlog" in d["decision"] and "不" in d["decision"] and "Claude path" in d["decision"]
        for d in hits
    ), "adr.json 條目未宣告『記入 backlog 且本場不動 Claude path』"


# --- 3) 標準 `核心改動:` 行能被真正的解析器抽出（可路由形式） --------------


def test_canonical_line_parses_via_real_parser():
    items = flow.parse_core_changes(CANONICAL_CORE_LINE)
    assert len(items) == 1, f"標準核心改動行應解析出 1 筆，實得 {items}"
    it = items[0]
    assert "Claude" in it["title"] and "去重" in it["title"]
    # 標籤缺省→預設 P1/improvement（flow.parse_core_changes docstring）。
    assert it["priority"] == 1
    assert it["type"] == "improvement"


# --- 4) 端到端：走生產路由真的落成一張 source=core 的核心 backlog 票 -------


def test_routes_into_core_backlog_end_to_end(core_state):
    items = flow.parse_core_changes(CANONICAL_CORE_LINE)
    routed = backlog.route_core_changes(items)
    assert routed == 1, "核心改動未被路由進核心 backlog"

    tasks = backlog.list_tasks()  # 省略 state_dir = 讀核心 backlog（已被 fixture 指向 tmp）
    core_tickets = [t for t in tasks if t.get("source") == "core"]
    assert len(core_tickets) == 1, f"核心 backlog 未出現 1 張 source=core 票：{tasks}"
    t = core_tickets[0]
    assert "Claude" in t["title"] and "去重" in t["title"]
    assert t["status"] == "pending", "新票應為 pending 等待 autopilot 認領"


# --- 5) 邊界：核心票不得被誤路由進專案 backlog --------------------------------


def test_not_routed_to_project_backlog(core_state, tmp_path):
    """專案 backlog（自帶 state_dir）不應因核心改動而新增任何項目。"""
    proj_dir = tmp_path / "project_state"
    items = flow.parse_core_changes(CANONICAL_CORE_LINE)
    backlog.route_core_changes(items)  # 走核心路由（不帶 state_dir）

    proj_tasks = backlog.list_tasks(state_dir=proj_dir)
    assert proj_tasks == [], "核心改動被誤路由進專案 backlog（雙軌路由破口）"


# --- 6) 已知限制黑樣本：committed 反引號/非行首形式不可就地再路由 -----------


def test_recorded_inline_form_is_not_routable_black_sample():
    """鎖定已知限制：DECISIONS.md/adr.json 內嵌的反引號形式，parse_core_changes
    回 []——它是『人類可讀記錄』而非『可重解析的路由來源』。

    若哪天有人改 regex 或記錄格式讓這條變成可解析，本測試會紅，提醒重新評估
    『重解析 doc 即重新路由』是否會造成核心 repo 重複/空轉 PR。
    """
    assert (
        flow.parse_core_changes(RECORDED_INLINE_FORM) == []
    ), "committed 內嵌形式現在可被解析——需重新評估重複路由風險"
