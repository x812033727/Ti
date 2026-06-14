"""QA 任務 #2 契約：strict+root 下 owner==0 且 0o600 的真實落地驗證。

對應驗收標準 #6 與 QA 子題「收集乾淨度與假紅隔離」：
- strict+root：history meta/events 與 backlog.json 真實 owner==0 且 mode==0o600。
- 區分 root / 非 root：非 root 環境明確 skip（非偽裝通過），不誤判為失敗。
- 另以 monkeypatch 模擬 owner 做「不依賴執行身分」的確定性驗證（root/非 root 皆跑）。
"""

from __future__ import annotations

import os
import stat
import types

import pytest

from studio import backlog, config, history, secure_write

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
skip_non_root = pytest.mark.skipif(
    not IS_ROOT, reason="strict 真實 owner 驗證需 root（非 root 明確 skip）"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "require_chown_mode", lambda: "strict")
    yield


def _assert_root_owned_0600(path):
    st = os.stat(path)
    assert st.st_uid == 0, f"{path} owner 非 root（uid={st.st_uid}）"
    assert stat.S_IMODE(st.st_mode) == 0o600, f"{path} mode={oct(stat.S_IMODE(st.st_mode))}"


# ---- 真實落地（需 root）----


@skip_non_root
def test_history_meta_events_root_owned_real():
    history.start_session("ses", "需求")
    history.record_event("ses", {"type": "done", "payload": {"completed": True}})
    history.finish_session("ses")
    _assert_root_owned_0600(config.HISTORY_ROOT / "ses.meta.json")
    _assert_root_owned_0600(config.HISTORY_ROOT / "ses.jsonl")


@skip_non_root
def test_backlog_json_root_owned_real():
    backlog.add("任務", state_dir=None)
    _assert_root_owned_0600(config.AUTOPILOT_STATE_DIR / "backlog.json")


# ---- 確定性驗證（monkeypatch 模擬 owner，root/非 root 皆跑）----


def test_strict_owner_check_simulated(monkeypatch, tmp_path):
    """模擬 fchown 成功且 fstat owner==0 → 寫入成功；owner≠0 → raise。"""
    monkeypatch.setattr(secure_write.os, "fchown", lambda fd, u, g: None)
    monkeypatch.setattr(
        secure_write.os, "fstat", lambda fd: types.SimpleNamespace(st_uid=0, st_nlink=1)
    )
    target = tmp_path / "ok.json"
    secure_write.secure_write_root(target, b"x", require_chown="strict")
    assert target.exists()

    monkeypatch.setattr(
        secure_write.os, "fstat", lambda fd: types.SimpleNamespace(st_uid=1234, st_nlink=1)
    )
    with pytest.raises(secure_write.SecureWriteError):
        secure_write.secure_write_root(tmp_path / "bad.json", b"x", require_chown="strict")
    assert not (tmp_path / "bad.json").exists()
