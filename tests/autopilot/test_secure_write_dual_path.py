"""任務 #3 契約：history 雙寫路徑（meta 覆寫 / events append）一致性。

對應驗收標準 #6 與架構子題「雙寫接點一致性」：
- meta 與 events 共用同一模式來源（config.require_chown_mode）。
- events.jsonl 由 start_session 以 secure_write_root 建立（root owner）；append 不破壞 owner
  （維持 .open("a")，不走覆寫語意的 secure_write_root）。
- record_event guard：未初始化（未 start_session）即 append → raise RuntimeError，早死。
"""

from __future__ import annotations

import pytest

from studio import config, history, secure_write


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    yield


def test_start_session_inits_events_via_secure_write(monkeypatch):
    calls = []
    real = history.secure_write_root

    def spy(path, data, **kw):
        calls.append(str(path))
        return real(path, data, **kw)

    monkeypatch.setattr(config, "require_chown_mode", lambda: "off")
    monkeypatch.setattr(history, "secure_write_root", spy)
    history.start_session("s1", "需求")
    # events.jsonl 與 meta 皆經 secure_write_root（同一寫入來源）
    assert any(p.endswith("s1.jsonl") for p in calls)
    assert any(p.endswith("s1.meta.json") for p in calls)


def test_record_event_guard_raises_when_uninitialized(monkeypatch):
    monkeypatch.setattr(config, "require_chown_mode", lambda: "off")
    config.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    # 未呼叫 start_session → events 檔不存在 → guard raise
    with pytest.raises(RuntimeError):
        history.record_event("ghost", {"type": "x"})


def test_events_append_does_not_rewrite(monkeypatch):
    """append 維持多行語意，不被 secure_write_root（覆寫）清空。"""
    monkeypatch.setattr(config, "require_chown_mode", lambda: "off")
    history.start_session("s2", "需求")
    history.record_event("s2", {"type": "a"})
    history.record_event("s2", {"type": "b"})
    events = history.load_events("s2")
    assert [e["type"] for e in events] == ["a", "b"]  # 兩行都在，未被覆寫


def test_append_preserves_owner_simulated(monkeypatch, tmp_path):
    """append 用 open('a')，不改 owner：對既有 root-owned 檔追加仍是同一 owner。

    以「append 不經 secure_write_root」為不變量驗證：record_event 期間不應呼叫
    secure_write_root（避免覆寫清空與重設 owner）。
    """
    monkeypatch.setattr(config, "require_chown_mode", lambda: "off")
    history.start_session("s3", "需求")
    called = []
    monkeypatch.setattr(history, "secure_write_root", lambda *a, **k: called.append(1))
    history.record_event("s3", {"type": "x"})
    assert called == []  # append 路徑完全不碰 secure_write_root


def test_meta_and_events_same_mode_source(monkeypatch):
    """strict + chown 失敗：meta 寫入即 raise（與 events 同一模式來源，不一寬一嚴）。"""
    monkeypatch.setattr(config, "require_chown_mode", lambda: "strict")

    def boom(fd, u, g):
        raise OSError("EPERM")

    monkeypatch.setattr(secure_write.os, "fchown", boom)
    # start_session 先建 events（secure_write_root strict）→ chown 失敗即 raise
    with pytest.raises(secure_write.SecureWriteError):
        history.start_session("s4", "需求")
