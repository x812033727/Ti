"""claude_accounts：多訂閱帳號標籤檔的列舉、在線標記與切換（換檔）。

純檔案操作，monkeypatch config.CLAUDE_CREDENTIALS_FILE 指向 tmp，不碰真憑證。
"""

from __future__ import annotations

import json

import pytest

from studio import claude_accounts, config


def _cred(token: str, sub: str = "max", exp: float | None = None) -> str:
    oauth: dict = {"accessToken": token, "subscriptionType": sub}
    if exp is not None:
        oauth["expiresAt"] = exp
    return json.dumps({"claudeAiOauth": oauth})


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """憑證目錄指向 tmp；預設不建任何檔，由各測試自行佈置。"""
    monkeypatch.setattr(config, "CLAUDE_CREDENTIALS_FILE", tmp_path / ".credentials.json")
    return tmp_path


def _write(tmp_path, name: str, content: str) -> None:
    (tmp_path / name).write_text(content, encoding="utf-8")


def test_list_accounts_empty_when_no_labels(_isolate):
    assert claude_accounts.list_accounts() == []


def test_list_accounts_reports_label_sub_and_active(_isolate):
    tmp = _isolate
    _write(tmp, ".credentials.acct-A.json", _cred("tA", "max"))
    _write(tmp, ".credentials.acct-B.json", _cred("tB", "pro"))
    _write(tmp, ".credentials.active", "B")
    accts = claude_accounts.list_accounts()
    assert [a["label"] for a in accts] == ["A", "B"]  # 依 label 排序
    by = {a["label"]: a for a in accts}
    assert by["A"]["subscription"] == "max"
    assert by["B"]["subscription"] == "pro"
    assert by["B"]["active"] is True
    assert by["A"]["active"] is False
    assert by["A"]["cred_file"].endswith(".credentials.acct-A.json")


def test_list_accounts_skips_illegal_filenames(_isolate):
    tmp = _isolate
    _write(tmp, ".credentials.acct-A.json", _cred("tA"))
    # 含非法字元的 label 不應被列出（防路徑穿越/雜訊）
    _write(tmp, ".credentials.acct-..json", _cred("x"))
    labels = [a["label"] for a in claude_accounts.list_accounts()]
    assert labels == ["A"]


def test_active_label_missing_or_illegal_returns_none(_isolate):
    tmp = _isolate
    assert claude_accounts.active_label() is None  # 無 .active 檔
    _write(tmp, ".credentials.active", "../etc")
    assert claude_accounts.active_label() is None  # 非法內容


def test_switch_swaps_live_and_preserves_current(_isolate):
    tmp = _isolate
    # 現況：在線 A，線上檔已是 A 自動續期後的最新 token；acct-A 標籤檔停在較舊 token。
    _write(tmp, ".credentials.acct-A.json", _cred("A-old"))
    _write(tmp, ".credentials.acct-B.json", _cred("B-tok"))
    _write(tmp, ".credentials.json", _cred("A-fresh"))
    _write(tmp, ".credentials.active", "A")

    claude_accounts.switch("B")

    live = json.loads((tmp / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
    acct_a = json.loads((tmp / ".credentials.acct-A.json").read_text())["claudeAiOauth"][
        "accessToken"
    ]
    assert live == "B-tok"  # 線上換成 B
    assert acct_a == "A-fresh"  # 切走前把線上最新 token 存回 A 標籤檔
    assert claude_accounts.active_label() == "B"


def test_sync_active_label_writes_back_when_live_newer(_isolate):
    """線上檔 expiresAt 較新（CLI 自動續期過）→ 回寫在線 label 標籤檔並回 True。"""
    tmp = _isolate
    _write(tmp, ".credentials.acct-A.json", _cred("A-old", exp=1_000))
    _write(tmp, ".credentials.json", _cred("A-fresh", exp=2_000))
    _write(tmp, ".credentials.active", "A")

    assert claude_accounts.sync_active_label() is True

    oauth = json.loads((tmp / ".credentials.acct-A.json").read_text())["claudeAiOauth"]
    assert oauth["accessToken"] == "A-fresh"
    assert oauth["expiresAt"] == 2_000


def test_sync_active_label_noop_when_label_not_older(_isolate):
    """標籤檔較新或相同 expiresAt → 不動檔案、回 False。"""
    tmp = _isolate
    for label_exp in (3_000, 2_000):  # 較新 / 相同
        _write(tmp, ".credentials.acct-A.json", _cred("A-label", exp=label_exp))
        _write(tmp, ".credentials.json", _cred("A-live", exp=2_000))
        _write(tmp, ".credentials.active", "A")

        assert claude_accounts.sync_active_label() is False
        oauth = json.loads((tmp / ".credentials.acct-A.json").read_text())["claudeAiOauth"]
        assert oauth["accessToken"] == "A-label"  # 標籤檔未被覆蓋


def test_sync_active_label_false_when_missing_or_broken(_isolate):
    """無在線 label／檔案缺失／檔案壞掉 → 一律回 False 不炸。"""
    tmp = _isolate
    assert claude_accounts.sync_active_label() is False  # 無 .active

    _write(tmp, ".credentials.active", "A")
    assert claude_accounts.sync_active_label() is False  # 標籤檔與線上檔皆缺

    _write(tmp, ".credentials.acct-A.json", _cred("A-tok", exp=1_000))
    assert claude_accounts.sync_active_label() is False  # 線上檔缺

    _write(tmp, ".credentials.json", "{壞掉的 json")
    assert claude_accounts.sync_active_label() is False  # 線上檔壞掉（expiresAt 讀不到）

    _write(tmp, ".credentials.json", _cred("A-new"))  # 線上檔沒有 expiresAt
    assert claude_accounts.sync_active_label() is False


def test_switch_rejects_unknown_and_illegal_label(_isolate):
    tmp = _isolate
    _write(tmp, ".credentials.acct-A.json", _cred("tA"))
    with pytest.raises(ValueError):
        claude_accounts.switch("Z")  # 無此標籤檔
    with pytest.raises(ValueError):
        claude_accounts.switch("../x")  # 非法 label


# --- pick_account：雙帳號分配的純決策（無 I/O，不碰檔案）--------------------
# v4 優先序：安全上限 > 7d 早重置多吃 > 5h 早重置多吃 > 負載平均分配。


def _w(
    fh: float | None = None,
    sd: float | None = None,
    fhr: float | None = None,
    sdr: float | None = None,
    sc: float | None = None,
) -> dict:
    """usages 值：兩額度窗用量 + 兩窗重置時間（epoch）+ 可選 scoped（Fable 週限）用量%。"""
    d = {"five_hour": fh, "seven_day": sd, "five_hour_reset": fhr, "seven_day_reset": sdr}
    if sc is not None:
        d["scoped"] = sc
    return d


def _pick(
    usages,
    active,
    preferred="B",
    threshold=95.0,
    margin=10.0,
    reset_edge=900.0,
    reset_edge_7d=21600.0,
    scoped_threshold=95.0,
):
    return claude_accounts.pick_account(
        usages,
        active,
        preferred,
        threshold,
        margin,
        reset_edge,
        reset_edge_7d,
        scoped_threshold=scoped_threshold,
    )


def test_pick_account_balances_when_gap_reaches_margin():
    """平均分配主規則：在線 B 負載比 A 高出 ≥margin → 切 A 攤平用量（未達上限也切）。"""
    assert _pick({"A": _w(18.0, 8.0), "B": _w(40.0, 12.0)}, "B") == "A"


def test_pick_account_load_is_max_of_windows():
    """負載＝5h/7d 取最大：B 的 7d 45 才是負載（5h 僅 30），比 A(25) 高 ≥margin → 切。"""
    assert _pick({"A": _w(25.0, 20.0), "B": _w(30.0, 45.0)}, "B") == "A"


def test_pick_account_gap_below_margin_stays():
    """遲滯：負載差 <margin → 不切（兩帳號負載相近時避免頻繁互切、每次切換都要重啟）。"""
    assert _pick({"A": _w(35.0), "B": _w(40.0)}, "B") is None
    assert _pick({"A": _w(40.0), "B": _w(35.0)}, "A") is None  # 反向同理：也不為回 B 而切


def test_pick_account_gap_exactly_margin_switches():
    """負載差恰等於 margin 即切（≥ 語意）。"""
    assert _pick({"A": _w(20.0), "B": _w(30.0)}, "B") == "A"


def test_pick_account_equal_load_stays_put():
    """同分：best 依 tie-break 是 preferred B，但差距 0 <margin → 留在原帳號不切。"""
    assert _pick({"A": _w(20.0), "B": _w(20.0)}, "A") is None
    assert _pick({"A": _w(20.0), "B": _w(20.0)}, "B") is None


def test_pick_account_tiebreak_prefers_preferred_then_alpha():
    """同分 tie-break：preferred 優先、再字母序。"""
    # A、B 同為 10 → best=B（preferred）；在線 C 高出 ≥margin → 切 B
    assert _pick({"A": _w(10.0), "B": _w(10.0), "C": _w(50.0)}, "C") == "B"
    # preferred B 不在同分組：A、C 同為 10 → 字母序取 A
    assert _pick({"A": _w(10.0), "B": _w(50.0), "C": _w(10.0)}, "B") == "A"


def test_pick_account_threshold_forces_switch_ignoring_margin():
    """安全上限：在線負載 ≥threshold → 即使差距 <margin 也強制切到仍低於上限者。"""
    assert _pick({"A": _w(94.0), "B": _w(96.0)}, "B") == "A"  # 差 2 <margin 仍切
    assert _pick({"A": _w(95.0), "B": _w(50.0)}, "A") == "B"  # 恰達上限（≥ 語意）→ 切
    assert _pick({"A": _w(90.0, 96.0), "B": _w(94.0)}, "A") == "B"  # 7d 窗達上限也算


def test_pick_account_none_when_all_at_threshold():
    """全部帳號負載 ≥threshold → 無候選 → None（交給既有 quota gate 睡到重置）。"""
    assert _pick({"A": _w(97.0), "B": _w(95.0)}, "B") is None


def test_pick_account_unavailable_active_switches_away():
    """在線帳號兩窗皆 None（額度查不到）→ 視為需要切走，切到可用帳號。"""
    assert _pick({"A": _w(50.0), "B": _w(None, None)}, "B") == "A"


def test_pick_account_active_missing_from_usages_switches_away():
    """在線 label 不在 usages（標籤檔遺失等）→ 同不可用，切到最低負載候選。"""
    assert _pick({"A": _w(10.0), "B": _w(20.0)}, "Z") == "A"


def test_pick_account_unavailable_account_not_a_target():
    """兩窗皆 None 的帳號不得為切換目標。"""
    assert _pick({"A": _w(None, None), "B": _w(96.0)}, "B") is None  # 唯一候選查不到 → 交給 gate
    assert _pick({"A": _w(None, None), "B": _w(40.0)}, "B") is None  # 在線健康、無其他候選 → 不動


def test_pick_account_none_window_ignored():
    """單一 None 窗忽略、以另一窗當負載：B 負載 80（7d）、A 20（5h）→ 差 ≥margin 切 A。"""
    assert _pick({"A": _w(20.0, None), "B": _w(None, 80.0)}, "B") == "A"


def test_pick_account_single_account_none():
    """只有一個帳號 → 無處可切，一律 None。"""
    assert _pick({"B": _w(99.0)}, "B") is None
    assert _pick({"B": _w(10.0)}, "B") is None


def test_pick_account_unknown_active_none():
    """在線 label 未知（None）→ 不動作，寧可不切也不亂切。"""
    assert _pick({"A": _w(10.0), "B": _w(20.0)}, None) is None


# --- pick_account：5h 早重置多吃（v4 第 2b 層；7d 皆未知時的日內節奏規則）----

_T = 1_800_000_000.0  # 固定 epoch 基準：決策只比較相對差，避免測試依賴當下時間


def test_pick_account_earlier_reset_wins_even_with_higher_load():
    """早重置 ≥edge 且非在線 → 切（即使負載較高，只要 <threshold：用量很快歸還、多吃划算）。"""
    usages = {"A": _w(60.0, fhr=_T + 600), "B": _w(30.0, fhr=_T + 3600)}
    assert _pick(usages, "B") == "A"  # A 早 3000 秒 ≥ edge 900，負載較高仍切


def test_pick_account_reset_gap_below_edge_falls_back_to_load():
    """早重置 <edge → 退回負載平衡規則（margin 遲滯照舊）。"""
    below = {"A": _w(18.0, fhr=_T + 600), "B": _w(40.0, fhr=_T + 1200)}
    assert _pick(below, "B") == "A"  # 重置差 600 <edge；負載差 22 ≥margin → 負載規則切
    close = {"A": _w(35.0, fhr=_T + 600), "B": _w(40.0, fhr=_T + 1200)}
    assert _pick(close, "B") is None  # 重置差 <edge 且負載差 5 <margin → 不切


def test_pick_account_reset_edge_exact_boundary():
    """edge 遲滯精確邊界：重置差恰等於 edge 即切（≥ 語意）、差 edge−1 退回負載規則。"""
    at_edge = {"A": _w(30.0, fhr=_T + 100), "B": _w(30.0, fhr=_T + 100 + 900)}
    assert _pick(at_edge, "B") == "A"  # 負載同分（負載規則不會切）→ 證明由重置規則觸發
    below_edge = {"A": _w(30.0, fhr=_T + 100), "B": _w(30.0, fhr=_T + 100 + 899)}
    assert _pick(below_edge, "B") is None  # 退回負載規則：同分 → 不切


def test_pick_account_reset_none_falls_back_to_load():
    """重置未知（None）→ 5h 規則不判定、下沉負載平衡：兩者重置**皆已知**才比較。

    v3 黏著 bug（已修）：單邊 None 被視為 +inf 直接比較，``inf - earliest >= edge``
    恆真——在線恰是唯一已知重置者時會恆回「留在線」，永不下沉到負載平衡。
    """
    assert _pick({"A": _w(18.0), "B": _w(40.0)}, "B") == "A"  # 皆 None → 純負載規則
    # A 已知、B 未知 → 非皆已知，下沉負載規則：差 22 ≥margin → 切 A（結論同 v3、理由不同）
    assert _pick({"A": _w(18.0, fhr=_T + 600), "B": _w(40.0)}, "B") == "A"
    # 在線 B 已知、A 未知：v3 會判 B「最早重置」黏死在線；v4 下沉負載規則 → 差 ≥margin 切 A
    assert _pick({"A": _w(18.0), "B": _w(40.0, fhr=_T + 600)}, "B") == "A"
    # 同上但負載差 <margin → 下沉後遲滯擋下，不切（黏著修正不會反向造成亂切）
    assert _pick({"A": _w(35.0), "B": _w(40.0, fhr=_T + 600)}, "B") is None


def test_pick_account_earliest_reset_is_active_stays():
    """早重置者＝在線 → 不切（留在線多吃），即使負載差 ≥margin 也不退回負載規則。"""
    usages = {"A": _w(18.0, fhr=_T + 3600), "B": _w(40.0, fhr=_T + 600)}
    assert _pick(usages, "B") is None


def test_pick_account_early_reset_but_exhausted_not_a_target():
    """最早重置但負載 ≥threshold → 不得為 target（安全上限優先於重置時間）。"""
    usages = {
        "A": _w(96.0, fhr=_T + 60),  # 全場最早重置但已達上限 → 排除在候選外
        "B": _w(50.0, fhr=_T + 7200),  # 在線
        "C": _w(20.0, fhr=_T + 3600),  # 候選中最早（比 B 早 3600 ≥edge）
    }
    assert _pick(usages, "B") == "C"


def test_pick_account_forced_switch_prefers_earlier_reset():
    """在線達上限的強制切同樣走重置優先：早重置 ≥edge 者勝過低負載者。"""
    usages = {
        "A": _w(60.0, fhr=_T + 600),
        "B": _w(96.0, fhr=_T + 300),  # 在線、達上限（不在候選）
        "C": _w(20.0, fhr=_T + 3600),
    }
    assert _pick(usages, "B") == "A"  # 負載規則會選 C；A 比 C 早 3000 秒 → 重置優先勝出


# --- pick_account v4：7d 早重置優先於 5h（週尺度稀缺資源先吃）-----------------


def test_pick_account_7d_priority_beats_5h_rule():
    """7d 早重置 ≥edge_7d → 切給它，即使 5h 規則會選另一邊（實案：B 的 7d 早 ~123h）。

    2026-07-04 晨間實測數據縮影：A 5h 較早重置（5h 規則會留 A），但 B 的 7d 窗早
    ~123 小時歸還——週尺度配額不先吃掉就是浪費，7d 規則必須壓過 5h。
    """
    usages = {
        "A": _w(53.0, 15.0, fhr=_T + 2.7 * 3600, sdr=_T + 163.7 * 3600),
        "B": _w(15.0, 13.0, fhr=_T + 3.9 * 3600, sdr=_T + 40.7 * 3600),
    }
    assert _pick(usages, "A") == "B"  # 5h 規則會判 A 最早（早 1.2h ≥edge）→ 7d 優先切 B


def test_pick_account_7d_earliest_is_active_stays():
    """7d 最早重置者＝在線 → 留在線多吃（None），不再下沉 5h／負載規則。"""
    usages = {
        "A": _w(53.0, 15.0, fhr=_T + 2.7 * 3600, sdr=_T + 163.7 * 3600),
        "B": _w(15.0, 13.0, fhr=_T + 3.9 * 3600, sdr=_T + 40.7 * 3600),
    }
    assert _pick(usages, "B") is None


def test_pick_account_7d_edge_exact_boundary():
    """edge_7d 遲滯精確邊界：7d 差恰等於 edge_7d 即觸發（≥ 語意）、差 edge_7d−1 下沉 5h。"""
    at_edge = {
        "A": _w(30.0, sdr=_T + 100, fhr=_T + 7200),
        "B": _w(30.0, sdr=_T + 100 + 21600, fhr=_T + 600),
    }
    # 負載同分（負載規則不切）、5h 規則會選 B（早 6600 ≥edge）→ 證明由 7d 規則選 A
    assert _pick(at_edge, "B") == "A"
    below_edge = {
        "A": _w(30.0, sdr=_T + 100, fhr=_T + 7200),
        "B": _w(30.0, sdr=_T + 100 + 21599, fhr=_T + 600),
    }
    assert _pick(below_edge, "B") is None  # 7d 差不足 → 下沉 5h：最早＝在線 B → 留著多吃


def test_pick_account_7d_one_side_none_falls_to_5h():
    """7d 單邊未知 → 7d 規則不判定（皆已知才比較），下沉 5h 規則決策。"""
    usages = {
        "A": _w(30.0, sdr=_T + 3600, fhr=_T + 600),
        "B": _w(30.0, fhr=_T + 7200),  # 7d 未知
    }
    assert _pick(usages, "B") == "A"  # 5h：A 早 6600 ≥edge → 切 A（7d 資訊不全不擋路）


def test_pick_account_7d_respects_threshold():
    """7d 最早重置但負載 ≥threshold → 不在候選，7d 規則在剩餘候選中運作（上限優先不變）。"""
    usages = {
        "A": _w(96.0, sdr=_T + 3600, fhr=_T + 600),  # 7d 全場最早但已達上限
        "B": _w(50.0, sdr=_T + 90 * 3600, fhr=_T + 7200),  # 在線
        "C": _w(40.0, sdr=_T + 40 * 3600, fhr=_T + 9000),  # 候選中 7d 最早（早 50h ≥edge_7d）
    }
    assert _pick(usages, "B") == "C"


# --- 第 1.5 層：scoped 週限（Fable）救援 ------------------------------------


def test_pick_account_scoped_rescue_switches_to_peer_with_headroom():
    """在線 A 的 scoped 撞滿(≥門檻)、B 仍有餘且全域可用 → 切 B(恢復 Fable 可用)。"""
    usages = {"A": _w(60.0, 60.0, sc=100.0), "B": _w(0.0, 0.0, sc=0.0)}
    assert _pick(usages, "A") == "B"


def test_pick_account_scoped_rescue_beats_7d_reset_preference():
    """scoped 救援優先於 7d 早重置:A 全域較早重置(純 v4 會留 A)但 A Fable 滿、B 新鮮 → 仍切 B。"""
    usages = {
        "A": _w(60.0, 60.0, sdr=_T + 3600, sc=100.0),  # 全域較早重置 + Fable 滿
        "B": _w(0.0, 0.0, sdr=_T + 90 * 3600, sc=0.0),  # 全域較晚重置 + Fable 新鮮
    }
    assert _pick(usages, "A") == "B"


def test_pick_account_scoped_no_rescue_when_peer_also_exhausted():
    """兩帳號 scoped 皆滿 → 無救援對象,下沉既有全域規則(此處負載差 <margin → 不切)。"""
    usages = {"A": _w(40.0, 40.0, sc=100.0), "B": _w(45.0, 45.0, sc=98.0)}
    assert _pick(usages, "A") is None


def test_pick_account_scoped_ignored_when_online_not_exhausted():
    """在線 scoped 未達門檻 → 本層不介入,照全域負載規則(B 高出 ≥margin → 切 A)。"""
    usages = {"A": _w(18.0, 8.0, sc=50.0), "B": _w(40.0, 12.0, sc=0.0)}
    assert _pick(usages, "B") == "A"


def test_pick_account_scoped_peer_global_capped_not_a_target():
    """對方 scoped 有餘但全域已達上限 → 不在候選,不得作救援目標(不切到爆帳號)。"""
    usages = {"A": _w(30.0, 30.0, sc=100.0), "B": _w(96.0, 50.0, sc=0.0)}
    assert _pick(usages, "A") is None


def test_pick_account_scoped_absent_field_is_backward_compatible():
    """未填 scoped 欄位(None)→ 本層完全略過,決策與純 v4 一致。"""
    usages = {"A": _w(18.0, 8.0), "B": _w(40.0, 12.0)}
    assert _pick(usages, "B") == "A"  # 同 test_pick_account_balances_when_gap_reaches_margin


def test_pick_account_scoped_tiebreak_prefers_preferred_then_alpha():
    """多個救援對象 scoped 同分 → preferred 優先、再字母序。"""
    usages = {
        "A": _w(50.0, 50.0, sc=100.0),  # 在線,Fable 滿
        "B": _w(10.0, 10.0, sc=0.0),
        "C": _w(10.0, 10.0, sc=0.0),
    }
    assert _pick(usages, "A", preferred="C") == "C"  # B、C scoped 同 0 → preferred C


# --- scoped_used_pct：scoped 比對 SSOT --------------------------------------


def test_scoped_used_pct_matches_display_name_in_model_id():
    """display_name(小寫)出現在 model id 內即命中,回 used_percentage。"""
    mu = {"Fable": {"used_percentage": 87.0, "reset_at": None}}
    assert claude_accounts.scoped_used_pct("claude-fable-5", mu) == 87.0


def test_scoped_used_pct_none_when_no_match_or_bad_input():
    """無對應模型 / 空 / 非 dict / 用量非數字 → None。"""
    mu = {"Fable": {"used_percentage": 87.0}}
    assert claude_accounts.scoped_used_pct("claude-opus-4-8", mu) is None
    assert claude_accounts.scoped_used_pct("claude-fable-5", None) is None
    assert claude_accounts.scoped_used_pct("", mu) is None
    assert (
        claude_accounts.scoped_used_pct("claude-fable-5", {"Fable": {"used_percentage": None}})
        is None
    )


# --- pin（手動模式釘選）------------------------------------------------------


def test_pinned_label_missing_or_illegal_returns_none(_isolate):
    tmp = _isolate
    assert claude_accounts.pinned_label() is None  # 無 pin 檔＝自動模式
    _write(tmp, ".credentials.pin", "../etc")
    assert claude_accounts.pinned_label() is None  # 非法內容視同無釘選


def test_set_pinned_writes_and_reads_back(_isolate):
    tmp = _isolate
    claude_accounts.set_pinned("A")
    assert (tmp / ".credentials.pin").read_text(encoding="utf-8") == "A"
    assert claude_accounts.pinned_label() == "A"
    claude_accounts.set_pinned("B")  # 覆寫＝last-write-wins
    assert claude_accounts.pinned_label() == "B"


def test_set_pinned_none_clears_even_when_missing(_isolate):
    tmp = _isolate
    claude_accounts.set_pinned(None)  # pin 檔不存在也不得炸
    claude_accounts.set_pinned("A")
    claude_accounts.set_pinned(None)
    assert not (tmp / ".credentials.pin").exists()
    assert claude_accounts.pinned_label() is None


def test_set_pinned_illegal_label_raises(_isolate):
    with pytest.raises(ValueError):
        claude_accounts.set_pinned("../etc")


def test_label_exists_requires_valid_label_and_file(_isolate):
    tmp = _isolate
    assert claude_accounts.label_exists("A") is False  # 憑證檔不存在
    _write(tmp, ".credentials.acct-A.json", _cred("tA"))
    assert claude_accounts.label_exists("A") is True
    assert claude_accounts.label_exists("../etc") is False  # 非法 label 不碰檔案系統


def test_list_accounts_reports_pinned(_isolate):
    tmp = _isolate
    _write(tmp, ".credentials.acct-A.json", _cred("tA"))
    _write(tmp, ".credentials.acct-B.json", _cred("tB"))
    by = {a["label"]: a for a in claude_accounts.list_accounts()}
    assert by["A"]["pinned"] is False and by["B"]["pinned"] is False  # 無 pin 全 False
    claude_accounts.set_pinned("B")
    by = {a["label"]: a for a in claude_accounts.list_accounts()}
    assert by["B"]["pinned"] is True
    assert by["A"]["pinned"] is False
