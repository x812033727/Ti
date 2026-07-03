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


# --- pick_account：雙帳號負載平均分配的純決策（無 I/O，不碰檔案）------------


def _w(fh: float | None = None, sd: float | None = None) -> dict:
    """兩額度窗的 usages 值：{"five_hour": fh, "seven_day": sd}。"""
    return {"five_hour": fh, "seven_day": sd}


def _pick(usages, active, preferred="B", threshold=95.0, margin=10.0):
    return claude_accounts.pick_account(usages, active, preferred, threshold, margin)


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
