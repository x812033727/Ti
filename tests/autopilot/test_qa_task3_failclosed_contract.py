"""QA 驗收：任務 #3「fail-closed 行為」。

聚焦 strict 模式的「失敗契約」：chown 失敗或擁有者驗證不過時，該次寫入必須
被「視為失敗並回報」（raise 明確例外），絕不靜默通過、也不得留下半成品或
覆寫既有正常檔。warn/off 不在 fail-closed 範圍（作為過渡選項）。

對應 task #3 驗收標準：
  - 模擬 chown 失敗：strict 回傳「該次寫入失敗」且有明確錯誤訊息，不回報成功。
  - 模擬驗證不過（owner!=root 等）：strict fail-closed 拒絕並回報，不降級接受。
  - 不得靜默通過：strict 失敗時函式必 raise，絕不正常返回 None。
"""

from __future__ import annotations

import os

import pytest

from studio.secure_write import SecureWriteError, secure_write_root


def _force_fchown_fail(monkeypatch):
    monkeypatch.setattr(
        os, "fchown", lambda fd, u, g: (_ for _ in ()).throw(PermissionError("EPERM"))
    )


def _force_owner_nonroot(monkeypatch):
    """chown 不報錯，但 fstat 回報 uid!=0（模擬驗證不過）。"""
    monkeypatch.setattr(os, "fchown", lambda fd, u, g: None)
    real_fstat = os.fstat

    class FakeStat:
        def __init__(self, st):
            self.st_uid = 1000
            self.st_nlink = 1
            self.st_mode = st.st_mode

    monkeypatch.setattr(os, "fstat", lambda fd: FakeStat(real_fstat(fd)))


# --- chown 失敗：strict 必 raise，且明確回報、不靜默通過 -----------------
def test_chown_fail_strict_raises_not_silent(tmp_path, monkeypatch):
    _force_fchown_fail(monkeypatch)
    target = tmp_path / "state.json"
    with pytest.raises(SecureWriteError):
        secure_write_root(target, b"payload", require_chown="strict")
    # 不靜默通過：目標檔不得被建立（rename 永遠到不了）
    assert not target.exists()


def test_chown_fail_message_is_actionable(tmp_path, monkeypatch):
    _force_fchown_fail(monkeypatch)
    target = tmp_path / "state.json"
    with pytest.raises(SecureWriteError) as ei:
        secure_write_root(target, b"x", require_chown="strict")
    msg = str(ei.value)
    assert str(target) in msg  # 含路徑
    assert "chown" in msg.lower()  # 含原因
    assert msg.strip() != ""  # 非空訊息


# --- 驗證不過（owner!=root）：strict fail-closed ------------------------
def test_owner_verify_fail_strict_raises(tmp_path, monkeypatch):
    _force_owner_nonroot(monkeypatch)
    target = tmp_path / "state"
    with pytest.raises(SecureWriteError) as ei:
        secure_write_root(target, b"x", require_chown="strict")
    assert "1000" in str(ei.value)  # 回報實際 owner
    assert not target.exists()


# --- 關鍵 fail-closed 性質：失敗不得覆寫/破壞既有正常檔 ------------------
def test_strict_failure_does_not_clobber_existing_file(tmp_path, monkeypatch):
    """既有目標檔內容在 strict 寫入失敗後必須原封不動（不破壞、不半截上線）。"""
    target = tmp_path / "state.json"
    target.write_bytes(b"GOOD-OLD-CONTENT")
    _force_fchown_fail(monkeypatch)
    with pytest.raises(SecureWriteError):
        secure_write_root(target, b"NEW-BAD-WRITE", require_chown="strict")
    assert target.read_bytes() == b"GOOD-OLD-CONTENT"  # 既有檔未被觸碰


def test_strict_failure_leaves_no_tmp_litter(tmp_path, monkeypatch):
    """失敗後不得殘留 .tmp 暫存檔（半成品）。"""
    _force_fchown_fail(monkeypatch)
    with pytest.raises(SecureWriteError):
        secure_write_root(tmp_path / "s", b"x", require_chown="strict")
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"殘留檔案: {leftovers}"


# --- 「不靜默通過」的硬性證明：strict 失敗時函式不會正常返回 ------------
def test_strict_never_returns_on_failure(tmp_path, monkeypatch):
    _force_fchown_fail(monkeypatch)
    raised = False
    ret = "sentinel"
    try:
        ret = secure_write_root(tmp_path / "s", b"x", require_chown="strict")
    except SecureWriteError:
        raised = True
    assert raised is True
    assert ret == "sentinel"  # 從未走到 return（沒有靜默成功）


# --- 對照：warn / off 不屬 fail-closed（確認 strict 才擋） ---------------
def test_warn_does_not_raise_on_chown_fail(tmp_path, monkeypatch):
    _force_fchown_fail(monkeypatch)
    target = tmp_path / "s"
    secure_write_root(target, b"x", require_chown="warn")  # 不 raise
    assert target.exists()


def test_off_does_not_raise_on_chown_fail(tmp_path, monkeypatch):
    # off 不應呼叫 fchown，故即使 mock 會丟錯也不觸發
    monkeypatch.setattr(
        os, "fchown", lambda *a: (_ for _ in ()).throw(PermissionError("EPERM"))
    )
    target = tmp_path / "s"
    secure_write_root(target, b"x", require_chown="off")
    assert target.exists()


# --- 預設（不帶 require_chown）採 config 預設 strict，失敗即 fail-closed --
def test_default_uses_strict_and_failcloses(tmp_path, monkeypatch):
    monkeypatch.delenv("TI_REQUIRE_CHOWN", raising=False)
    import importlib

    from studio import config

    importlib.reload(config)
    _force_fchown_fail(monkeypatch)
    with pytest.raises(SecureWriteError):
        secure_write_root(tmp_path / "s", b"x")  # 不帶參數 → 採 config 預設 strict
    importlib.reload(config)
