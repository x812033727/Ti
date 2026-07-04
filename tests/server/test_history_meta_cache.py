"""history.list_sessions 每檔 mtime 快取行為守護。

快取契約：未變動的 meta 檔不重讀 JSON（spy `_read_meta_file`）、變動（mtime/size）即失效、
刪檔即從結果與快取逐出、不同 HISTORY_ROOT 互不污染、壞檔不入快取、排序契約
（started_at 新→舊）不變。
"""

from __future__ import annotations

import pytest

from studio import config, history


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    history._reset_meta_cache()
    yield
    history._reset_meta_cache()


class ReadSpy:
    """包住 _read_meta_file 記錄實際讀檔次數。"""

    def __init__(self, monkeypatch) -> None:
        self.calls = 0
        real = history._read_meta_file

        def spy(path):
            self.calls += 1
            return real(path)

        monkeypatch.setattr(history, "_read_meta_file", spy)


def test_cache_hit_skips_reread(monkeypatch):
    for i in range(3):
        history.start_session(f"s{i}", f"req-{i}")
    first = history.list_sessions()
    assert len(first) == 3

    spy = ReadSpy(monkeypatch)
    second = history.list_sessions()
    assert spy.calls == 0  # 全部命中快取，不重讀
    assert second == first


def test_meta_change_invalidates_single_file(monkeypatch):
    for i in range(3):
        history.start_session(f"s{i}", f"req-{i}")
    history.list_sessions()  # 暖快取

    # 直接經 _write_meta 改動（finish_session 內部會經 enforce_retention 提前刷新快取，
    # 不適合當 spy 樣本）；_write_meta → secure_write_root rename 必刷 mtime。
    meta = history.get_meta("s1")
    meta["status"] = "completed"
    history._write_meta("s1", meta)

    spy = ReadSpy(monkeypatch)
    metas = history.list_sessions()
    assert spy.calls == 1  # 只重讀變動的那一檔
    status = {m["session_id"]: m["status"] for m in metas}
    assert status["s1"] == "completed"  # 新狀態已反映
    assert status["s0"] == "running" and status["s2"] == "running"


def test_new_and_deleted_sessions_reflected():
    history.start_session("a", "req-a")
    assert {m["session_id"] for m in history.list_sessions()} == {"a"}

    history.start_session("b", "req-b")
    assert {m["session_id"] for m in history.list_sessions()} == {"a", "b"}

    history.finish_session("a")
    assert history.delete_session("a")
    assert {m["session_id"] for m in history.list_sessions()} == {"b"}
    # 刪檔即從快取逐出（不殘留殭屍 key）
    assert not any("a.meta.json" in k for k in history._meta_cache)


def test_history_root_switch_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "h1")
    history.start_session("one", "req")
    assert {m["session_id"] for m in history.list_sessions()} == {"one"}

    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "h2")
    history.start_session("two", "req")
    # 切換目錄後只看見新目錄的 session，不滲漏 h1 的快取
    assert {m["session_id"] for m in history.list_sessions()} == {"two"}

    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "h1")
    assert {m["session_id"] for m in history.list_sessions()} == {"one"}


def test_corrupt_meta_skipped_and_not_cached(monkeypatch):
    history.start_session("good", "req")
    bad = config.HISTORY_ROOT / "bad.meta.json"
    bad.write_text("{ 不是 JSON", encoding="utf-8")

    assert {m["session_id"] for m in history.list_sessions()} == {"good"}
    assert not any("bad.meta.json" in k for k in history._meta_cache)

    # 壞檔修好後（內容變動）下次即讀到，證明沒把「壞」快取成殭屍
    bad.write_text('{"session_id": "bad", "started_at": 1.0}', encoding="utf-8")
    assert {m["session_id"] for m in history.list_sessions()} == {"good", "bad"}


def test_sort_contract_newest_first():
    import json

    root = config.HISTORY_ROOT
    root.mkdir(parents=True, exist_ok=True)
    for sid, ts in (("old", 100.0), ("new", 300.0), ("mid", 200.0)):
        (root / f"{sid}.meta.json").write_text(
            json.dumps({"session_id": sid, "started_at": ts}), encoding="utf-8"
        )
    assert [m["session_id"] for m in history.list_sessions()] == ["new", "mid", "old"]
    # 快取命中路徑同樣維持排序
    assert [m["session_id"] for m in history.list_sessions()] == ["new", "mid", "old"]
