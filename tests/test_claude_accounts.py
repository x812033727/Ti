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


# --- pick_account：雙帳號 95% 輪替的純決策（無 I/O，不碰檔案）--------------


def _pick(usages, active, preferred="B", threshold=95.0):
    return claude_accounts.pick_account(usages, active, preferred, threshold)


def test_pick_account_switches_away_when_active_hits_threshold():
    """規則 2：在線 B「恰達」95% 且 A 未達 → 切 A（門檻採 >=）。"""
    assert _pick({"A": 10.0, "B": 95.0}, "B") == "A"


def test_pick_account_switches_back_when_other_side_exhausted():
    """規則 2（雙向互切）：在線 A 達門檻且 B 未達 → 切回 B。"""
    assert _pick({"A": 96.0, "B": 40.0}, "A") == "B"


def test_pick_account_picks_lowest_usage_among_candidates():
    """規則 2：多帳號時切到未達門檻者中「用量最低」的那個。"""
    assert _pick({"A": 50.0, "B": 97.0, "C": 20.0}, "B") == "C"


def test_pick_account_none_when_all_exhausted():
    """規則 3：兩邊都 ≥95% → None（不切換，交給既有 quota gate 睡到重置）。"""
    assert _pick({"A": 97.0, "B": 95.0}, "B") is None
    assert _pick({"A": 95.0, "B": 99.0}, "A") is None


def test_pick_account_returns_to_preferred_after_reset():
    """規則 1：在線 A 未達門檻、且 preferred B 已降回門檻以下（重置）→ 回 B。"""
    assert _pick({"A": 50.0, "B": 3.0}, "A") == "B"


def test_pick_account_stays_when_preferred_still_exhausted():
    """在線 A 未達門檻、但 preferred B 仍 ≥95% → 不切（留在 A 繼續消化額度）。"""
    assert _pick({"A": 50.0, "B": 96.0}, "A") is None


def test_pick_account_noop_when_active_is_preferred_and_healthy():
    """在線即 preferred 且未達門檻 → 不切換。"""
    assert _pick({"A": 10.0, "B": 50.0}, "B") is None


def test_pick_account_single_account_none():
    """只有一個帳號 → 無處可切，一律 None。"""
    assert _pick({"B": 99.0}, "B") is None
    assert _pick({"B": 10.0}, "B") is None


def test_pick_account_unknown_usage_is_not_a_target():
    """None 用量＝查不到 → 不可作為切入目標；在線帳號查不到用量也不動作。"""
    assert _pick({"A": None, "B": 96.0}, "B") is None  # 目標查不到 → 不切入
    assert _pick({"A": 20.0, "B": None}, "B") is None  # 在線查不到 → 不動作
    assert _pick({"A": 10.0, "B": None}, "A") is None  # preferred 查不到 → 不回切


def test_pick_account_missing_preferred_or_unknown_active_none():
    """preferred 缺席 → 不回切；在線 label 未知（None／不在 usages）→ 不動作。"""
    assert _pick({"A": 10.0, "C": 20.0}, "A") is None  # preferred B 缺席
    assert _pick({"A": 96.0, "C": 20.0}, "A") == "C"  # 但規則 2 不依賴 preferred
    assert _pick({"A": 10.0, "B": 20.0}, None) is None  # 無在線 label
    assert _pick({"A": 10.0, "B": 20.0}, "Z") is None  # 在線 label 不在 usages
