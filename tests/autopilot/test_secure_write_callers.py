"""任務 #3/#4 契約：history._write_meta 與 backlog._save 走 secure_write_root。

對應驗收標準 #6：
- history meta 與 backlog.json 寫入皆經 secure_write_root（同一模式來源 config.require_chown_mode）。
- strict+chown 失敗時呼叫端往上拋 SecureWriteError，不靜默、不落地。
- off 模式呼叫端不拋。
- backlog._save 在 _locked() 範圍內呼叫 secure_write_root（無 TOCTOU）。
"""

from __future__ import annotations

import pytest

from studio import backlog, config, history, secure_write


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    yield


# ---- 接點確實走 secure_write_root ----


def test_write_meta_routes_through_secure_write(monkeypatch):
    seen = {}

    def spy(path, data, **kw):
        seen["path"] = str(path)
        seen["data"] = data

    monkeypatch.setattr(history, "secure_write_root", spy)
    history._write_meta("sess", {"k": 1})
    assert seen["path"].endswith("sess.meta.json")
    assert isinstance(seen["data"], bytes | bytearray)


def test_backlog_save_routes_through_secure_write(monkeypatch):
    seen = {}

    def spy(path, data, **kw):
        seen["path"] = str(path)
        seen["data"] = data

    monkeypatch.setattr(backlog, "secure_write_root", spy)
    backlog._save({"seq": 0, "tasks": []}, None)
    assert seen["path"].endswith("backlog.json")
    assert isinstance(seen["data"], bytes | bytearray)


# ---- strict + chown 失敗：呼叫端往上拋，不靜默、不落地 ----


def test_meta_strict_chown_fail_propagates(monkeypatch):
    monkeypatch.setattr(config, "require_chown_mode", lambda: "strict")

    def boom(fd, u, g):
        raise OSError("EPERM")

    monkeypatch.setattr(secure_write.os, "fchown", boom)
    config.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)  # production 由 start_session 建
    with pytest.raises(secure_write.SecureWriteError):
        history._write_meta("s2", {"x": 1})
    assert not (config.HISTORY_ROOT / "s2.meta.json").exists()


def test_backlog_strict_chown_fail_propagates(monkeypatch):
    monkeypatch.setattr(config, "require_chown_mode", lambda: "strict")

    def boom(fd, u, g):
        raise OSError("EPERM")

    monkeypatch.setattr(secure_write.os, "fchown", boom)
    config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    with pytest.raises(secure_write.SecureWriteError):
        backlog._save({"seq": 1, "tasks": []}, None)
    assert not (config.AUTOPILOT_STATE_DIR / "backlog.json").exists()


# ---- off 模式：呼叫端不拋（測試環境慣例值）----


def test_off_callers_do_not_raise(monkeypatch):
    monkeypatch.setattr(config, "require_chown_mode", lambda: "off")
    history.start_session("s3", "需求")
    history.record_event("s3", {"type": "done", "payload": {"completed": True}})
    history.finish_session("s3")
    t = backlog.add("任務X", state_dir=None)
    assert t is not None
    assert (config.AUTOPILOT_STATE_DIR / "backlog.json").exists()


# ---- backlog._save 在 lock 範圍內（add 路徑全程 _locked）----


def test_backlog_save_within_lock(monkeypatch):
    """真正驗證 _save 執行當下 backlog.lock 被持有（在 _locked 範圍內，無 TOCTOU）。

    flock 對「不同 open 描述」獨立判定：即使同進程，另開一個 fd 以 LOCK_NB 嘗試取
    同一把鎖，在 add() 仍持有期間應被拒（OSError）。以此證明 _save 確實落在鎖內，
    而非僅驗「_save 被呼叫」（後者在鎖外也會過）。
    """
    import fcntl

    real_save = backlog._save
    lock_state = {}

    def traced_save(data, state_dir):
        probe_path = backlog._lock_path(state_dir)
        with open(probe_path, "w") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
                lock_state["held"] = False  # 取得成功＝鎖沒被持有（壞）
            except OSError:
                lock_state["held"] = True  # 被拒＝add() 仍持有鎖（對）
        return real_save(data, state_dir)

    monkeypatch.setattr(config, "require_chown_mode", lambda: "off")
    monkeypatch.setattr(backlog, "_save", traced_save)
    backlog.add("鎖內寫入", state_dir=None)
    assert lock_state.get("held") is True
