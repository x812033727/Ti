"""QA 驗收：任務 #4「雙寫兩條後端路徑套用同一套嚴格檢查」。

兩條後端路徑：
  A. HISTORY_ROOT      —— studio.history（events 檔建立 + meta 寫入）
  B. AUTOPILOT_STATE_DIR —— studio.backlog（backlog.json）
兩者皆須經由唯一 choke point studio.secure_write.secure_write_root，採同一 config 預設
（strict），不得一寬一嚴；任一路徑驗證未通過即該操作整體失敗（raise），不會被當 root-only。

對應 task #4 驗收標準：
  - 雙寫兩條路徑皆套用同一檢查。
  - 單測證明任一路徑未通過即整體失敗，不會出現「一路徑非 root 卻被當 root-only」。
"""

from __future__ import annotations

import importlib
import inspect
import os

import pytest

from studio import backlog, config, history
from studio.secure_write import SecureWriteError


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """把兩條後端根目錄都指到 tmp，避免污染真實 state，且 config 預設回 strict。"""
    monkeypatch.setenv("TI_REQUIRE_CHOWN", "strict")
    importlib.reload(config)
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "autopilot")
    yield tmp_path
    importlib.reload(config)


def _fchown_fails(monkeypatch):
    monkeypatch.setattr(
        os, "fchown", lambda fd, u, g: (_ for _ in ()).throw(PermissionError("EPERM"))
    )


# --- 共用 choke point：兩條路徑都呼叫同一個 secure_write_root -----------
def test_both_paths_route_through_single_chokepoint():
    hist_src = inspect.getsource(history)
    bl_src = inspect.getsource(backlog)
    assert "secure_write.secure_write_root" in hist_src
    assert "secure_write.secure_write_root" in bl_src


def test_no_path_weakens_mode():
    """任一呼叫端都不得以 require_chown='off'/'warn' 弱化檢查（須採 config 預設）。"""
    for mod in (history, backlog):
        src = inspect.getsource(mod)
        assert 'require_chown="off"' not in src
        assert "require_chown='off'" not in src
        assert 'require_chown="warn"' not in src
        assert "require_chown='warn'" not in src


# --- 路徑 A（history）：非 root 時整體失敗，不靜默通過 ------------------
def test_history_path_failcloses(isolated_state, monkeypatch):
    _fchown_fails(monkeypatch)
    with pytest.raises(SecureWriteError):
        history.start_session("sess-A", "需求 A")
    # 不會留下「被當成 root-only」的 meta 檔
    assert not (config.HISTORY_ROOT / "sess-A.meta.json").exists()


# --- 路徑 B（backlog）：非 root 時整體失敗，不靜默通過 ------------------
def test_backlog_path_failcloses(isolated_state, monkeypatch):
    _fchown_fails(monkeypatch)
    with pytest.raises(SecureWriteError):
        backlog.add("任務 B", "detail")
    # backlog.json 不應被寫出（避免非 root 檔被當 root-only）
    assert not (config.AUTOPILOT_STATE_DIR / "backlog.json").exists()


# --- 對稱性：同一情境下兩路徑「同進同退」，不會一寬一嚴 -----------------
@pytest.mark.parametrize("which", ["history", "backlog"])
def test_either_path_failure_is_total(isolated_state, monkeypatch, which):
    """參數化證明：無論哪一條路徑，非 root 一律 raise（整體失敗），行為一致。"""
    _fchown_fails(monkeypatch)
    if which == "history":
        def op():
            history.start_session("s", "r")
        artifact = config.HISTORY_ROOT / "s.meta.json"
    else:
        def op():
            backlog.add("t")
        artifact = config.AUTOPILOT_STATE_DIR / "backlog.json"
    with pytest.raises(SecureWriteError):
        op()
    assert not artifact.exists()


# --- 正向（以 root）：兩路徑產出的檔皆 root-owned，確認一致為 root-only --
@pytest.mark.skipif(os.geteuid() != 0, reason="正向 root-owned 驗證需以 root 執行")
def test_both_paths_produce_root_owned_files(isolated_state):
    history.start_session("sess-ok", "需求")
    backlog.add("任務 ok")
    meta = config.HISTORY_ROOT / "sess-ok.meta.json"
    events = history._events_path("sess-ok")
    bl = config.AUTOPILOT_STATE_DIR / "backlog.json"
    for f in (meta, events, bl):
        assert f.exists(), f"{f} 未建立"
        st = os.lstat(f)
        assert st.st_uid == 0, f"{f} owner uid={st.st_uid}，期望 0/root"


# --- backlog 的 set_status 也走同一檢查（read-modify-write 第二入口）----
def test_backlog_set_status_also_failcloses(isolated_state, monkeypatch):
    # 先以正常方式建立一筆任務（root 下成功）
    if os.geteuid() != 0:
        pytest.skip("需先以 root 成功寫入再模擬後續失敗")
    task = backlog.add("待改狀態")
    assert task is not None
    # 之後 chown 失敗 → set_status 須整體失敗
    _fchown_fails(monkeypatch)
    with pytest.raises(SecureWriteError):
        backlog.set_status(task["id"], "done")
