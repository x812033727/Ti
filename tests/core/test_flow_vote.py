"""3-AI 表決純函式層（flow.py）測試：表決請求解析、選票解析、多數決計票、投票員挑選。

全部純函式、零 LLM／零副作用；orchestrator 端的表決編排見 test_orchestrator_vote.py。
"""

from __future__ import annotations

from studio.flow import parse_ballot, parse_vote_request, pick_vote_providers, tally_votes

# --- parse_vote_request -------------------------------------------------------


def test_parse_vote_request_basic():
    out = parse_vote_request("我無法決定。\n表決: 儲存層方案 | SQLite | JSON 檔")
    assert out == {"topic": "儲存層方案", "options": ["SQLite", "JSON 檔"]}


def test_parse_vote_request_three_options_and_fullwidth():
    # 全形冒號＋全形管線容錯；第三選項可選。
    out = parse_vote_request("表決：部署方式｜Docker｜systemd｜手動")
    assert out == {"topic": "部署方式", "options": ["Docker", "systemd", "手動"]}


def test_parse_vote_request_last_hit_wins():
    text = "表決: 舊議題 | A | B\n中間說明\n表決: 新議題 | X | Y"
    assert parse_vote_request(text)["topic"] == "新議題"
    assert parse_vote_request(text)["options"] == ["X", "Y"]


def test_parse_vote_request_too_few_options_returns_none():
    assert parse_vote_request("表決: 議題 | 只有一個選項") is None
    assert parse_vote_request("表決: 只有議題") is None
    # 空段落剔除後不足兩個選項 → None。
    assert parse_vote_request("表決: 議題 | A |  ") is None


def test_parse_vote_request_empty_topic_returns_none():
    assert parse_vote_request("表決:  | A | B") is None


def test_parse_vote_request_dedups_options_keeps_order():
    out = parse_vote_request("表決: 議題 | A | B | A | C")
    assert out["options"] == ["A", "B", "C"]


def test_parse_vote_request_no_marker_returns_none():
    assert parse_vote_request("一些無關文字") is None
    assert parse_vote_request("") is None
    assert parse_vote_request(None) is None


# --- parse_ballot -------------------------------------------------------------


def test_parse_ballot_exact_match():
    assert parse_ballot("理由如下。\n投票: SQLite", ["SQLite", "JSON 檔"]) == "SQLite"


def test_parse_ballot_fullwidth_colon_and_last_wins():
    assert parse_ballot("投票：A\n投票: B", ["A", "B"]) == "B"


def test_parse_ballot_fuzzy_match():
    # LLM 少字/加標點：相似度 ≥0.6 取最佳選項原文。
    assert parse_ballot("投票: SQLite。", ["SQLite", "JSON 檔"]) == "SQLite"
    assert (
        parse_ballot("投票: 用 Docker 部署", ["用 Docker 部署到雲端", "手動"])
        == "用 Docker 部署到雲端"
    )


def test_parse_ballot_unmatched_is_abstain():
    # 與所有選項都不像 → 棄權（空字串）。
    assert parse_ballot("投票: 完全無關的東西", ["SQLite", "JSON 檔"]) == ""


def test_parse_ballot_missing_marker_is_abstain():
    assert parse_ballot("我覺得都可以", ["A", "B"]) == ""
    assert parse_ballot("", ["A", "B"]) == ""
    assert parse_ballot(None, ["A", "B"]) == ""


def test_parse_ballot_empty_options_is_abstain():
    assert parse_ballot("投票: A", []) == ""


# --- tally_votes --------------------------------------------------------------


def _b(voter, provider, choice):
    return {"voter": voter, "provider": provider, "choice": choice}


def test_tally_votes_majority_wins():
    out = tally_votes(
        [
            _b("pm", "claude", "A"),
            _b("voter_codex", "codex", "B"),
            _b("voter_minimax", "minimax", "B"),
        ]
    )
    assert out == {"winner": "B", "counts": {"A": 1, "B": 2}, "tie": False}


def test_tally_votes_abstain_not_counted():
    out = tally_votes([_b("pm", "claude", "A"), _b("voter_codex", "codex", "")])
    assert out == {"winner": "A", "counts": {"A": 1}, "tie": False}


def test_tally_votes_tie_pm_vote_wins():
    # 三方各投一票（平手）→ PM 票定案、tie=True 標記。
    out = tally_votes(
        [
            _b("pm", "claude", "C"),
            _b("voter_codex", "codex", "A"),
            _b("voter_minimax", "minimax", "B"),
        ]
    )
    assert out["winner"] == "C" and out["tie"] is True
    assert out["counts"] == {"C": 1, "A": 1, "B": 1}


def test_tally_votes_tie_without_pm_returns_empty_winner():
    # 平手且 PM 棄權 → winner=""（交呼叫端降級兜底）。
    out = tally_votes(
        [
            _b("pm", "claude", ""),
            _b("voter_codex", "codex", "A"),
            _b("voter_minimax", "minimax", "B"),
        ]
    )
    assert out["winner"] == "" and out["tie"] is True


def test_tally_votes_all_abstain_or_empty():
    assert tally_votes([]) == {"winner": "", "counts": {}, "tie": False}
    assert tally_votes([_b("pm", "claude", "")]) == {"winner": "", "counts": {}, "tie": False}


# --- pick_vote_providers ------------------------------------------------------


def _digest():
    return {
        "claude": {"ready": True, "error": None, "max_used": 30, "soonest_reset": None},
        "codex": {"ready": True, "error": None, "max_used": 10, "soonest_reset": None},
        "minimax": {"ready": True, "error": None, "max_used": 50, "soonest_reset": None},
        "antigravity": {"ready": False, "error": None, "max_used": None, "soonest_reset": None},
    }


def test_pick_vote_providers_sorted_by_usage_excluding_pm():
    # 排除 PM 的 claude；就緒者按 max_used 升冪 → codex(10) < minimax(50)。
    assert pick_vote_providers(_digest(), exclude="claude") == ["codex", "minimax"]


def test_pick_vote_providers_excludes_constrained_error_not_ready():
    d = _digest()
    d["codex"]["max_used"] = 95  # 受限（≥90）
    d["minimax"]["error"] = "boom"  # 查詢異常
    # 只剩 claude 可用，但被排除 → 空。
    assert pick_vote_providers(d, exclude="claude") == []


def test_pick_vote_providers_insufficient_returns_actual():
    d = _digest()
    d["minimax"]["ready"] = False
    assert pick_vote_providers(d, exclude="claude") == ["codex"]  # 不足 2 → 回實際數


def test_pick_vote_providers_none_usage_treated_as_zero():
    d = _digest()
    d["antigravity"]["ready"] = True  # max_used=None → 視為 0（最寬鬆），排最前
    assert pick_vote_providers(d, exclude="claude") == ["antigravity", "codex"]


def test_pick_vote_providers_respects_n_and_empty_digest():
    assert pick_vote_providers(_digest(), exclude="claude", n=1) == ["codex"]
    assert pick_vote_providers(_digest(), exclude="claude", n=0) == []
    assert pick_vote_providers({}, exclude="claude") == []
    assert pick_vote_providers(None, exclude="") == []


def test_pick_vote_providers_exclude_case_insensitive():
    assert "claude" not in pick_vote_providers(_digest(), exclude=" Claude ")
