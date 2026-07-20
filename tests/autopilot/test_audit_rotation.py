"""audit.jsonl 壓實（輪替）守護——上輪明列的 known-limitation 補齊。

契約：超過 _AUDIT_MAX_BYTES 時，保留期（_AUDIT_KEEP_DAYS）外的舊紀錄搬到
audit.jsonl.old、現役檔原子重寫只留近期；未超標不動；全在保留期內不重寫；
壞行視為舊紀錄歸檔；壓實絕不影響每日 PR 計數口徑（今日紀錄恆在保留期內）。
"""

from __future__ import annotations

import json
import time

import pytest

from studio import autopilot, config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")


def _seed(n: int, ts: float, pr: int | None = 1) -> None:
    for i in range(n):
        autopilot._append_audit({"ts": ts, "task_id": i, "pr": pr})


def _lines(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_compact_archives_old_keeps_recent(monkeypatch):
    monkeypatch.setattr(autopilot, "_AUDIT_MAX_BYTES", 200)  # 幾筆即觸發
    old_ts = time.time() - (autopilot._AUDIT_KEEP_DAYS + 10) * 86400
    _seed(5, ts=old_ts)
    _seed(2, ts=time.time())  # 今日 2 筆（觸發壓實的 append 也在其中）

    path = autopilot._audit_path()
    archive = path.with_suffix(".jsonl.old")
    live = _lines(path)
    assert all(r["ts"] >= time.time() - autopilot._AUDIT_KEEP_DAYS * 86400 for r in live)
    assert archive.exists() and len(_lines(archive)) >= 5  # 舊紀錄全數歸檔
    assert len(live) + len(_lines(archive)) == 7  # 一筆不丟
    assert autopilot._todays_pr_count() == 2  # 計數口徑不受壓實影響


def test_below_threshold_untouched():
    _seed(3, ts=time.time() - (autopilot._AUDIT_KEEP_DAYS + 10) * 86400)
    path = autopilot._audit_path()
    assert len(_lines(path)) == 3  # 未超標：舊紀錄也原樣留著
    assert not path.with_suffix(".jsonl.old").exists()


def test_all_recent_oversize_not_rewritten(monkeypatch):
    """全部都在保留期內：寧可暫時超標也不丟計數窗口附近的紀錄。"""
    monkeypatch.setattr(autopilot, "_AUDIT_MAX_BYTES", 100)
    _seed(5, ts=time.time())
    path = autopilot._audit_path()
    assert len(_lines(path)) == 5
    assert not path.with_suffix(".jsonl.old").exists()


def test_corrupt_lines_archived(monkeypatch):
    monkeypatch.setattr(autopilot, "_AUDIT_MAX_BYTES", 150)
    _seed(1, ts=time.time())
    with autopilot._audit_path().open("a", encoding="utf-8") as f:
        f.write("{ 不是 JSON\n" * 5)
    _seed(1, ts=time.time())  # 觸發壓實

    path = autopilot._audit_path()
    assert len(_lines(path)) == 2  # 現役檔只剩可解析的近期紀錄
    archive_text = path.with_suffix(".jsonl.old").read_text(encoding="utf-8")
    assert "不是 JSON" in archive_text  # 壞行歸檔而非默默消失
    assert autopilot._todays_pr_count() == 2
