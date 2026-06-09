"""P4：history.busy_sessions 的 stale 判定——卡在 running 的死 session 不該算『進行中』。"""

from __future__ import annotations

import json
import os
import time

from studio import history


def _mk(root, sid, status, last_age_s):
    """造一個 session：meta(status) + events 檔，並把 events mtime 設成 last_age_s 秒前。"""
    (root / f"{sid}.meta.json").write_text(
        json.dumps(
            {"session_id": sid, "status": status, "started_at": time.time() - last_age_s - 1}
        ),
        encoding="utf-8",
    )
    ev = root / f"{sid}.jsonl"
    ev.write_text("{}\n", encoding="utf-8")
    t = time.time() - last_age_s
    os.utime(ev, (t, t))


def test_busy_excludes_stale_and_finished(tmp_path, monkeypatch):
    monkeypatch.setattr(history.config, "HISTORY_ROOT", tmp_path)
    _mk(tmp_path, "fresh", "running", 10)  # running + 10 秒前活動 → 真正進行中
    _mk(tmp_path, "stale", "running", 3600)  # running + 1 小時沒動 → stale，不算
    _mk(tmp_path, "done", "completed", 10)  # 非 running → 略過

    busy = history.busy_sessions(stale_after_s=1800)
    assert {m["session_id"] for m in busy} == {"fresh"}


def test_busy_empty_when_all_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(history.config, "HISTORY_ROOT", tmp_path)
    _mk(tmp_path, "dead", "running", 99999)
    assert history.busy_sessions(stale_after_s=1800) == []
