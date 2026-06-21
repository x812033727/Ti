"""claude_accounts：多訂閱帳號標籤檔的列舉、在線標記與切換（換檔）。

純檔案操作，monkeypatch config.CLAUDE_CREDENTIALS_FILE 指向 tmp，不碰真憑證。
"""

from __future__ import annotations

import json

import pytest

from studio import claude_accounts, config


def _cred(token: str, sub: str = "max") -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": token, "subscriptionType": sub}})


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


def test_switch_rejects_unknown_and_illegal_label(_isolate):
    tmp = _isolate
    _write(tmp, ".credentials.acct-A.json", _cred("tA"))
    with pytest.raises(ValueError):
        claude_accounts.switch("Z")  # 無此標籤檔
    with pytest.raises(ValueError):
        claude_accounts.switch("../x")  # 非法 label
