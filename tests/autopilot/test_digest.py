"""週報 digest(功能強化 D2):純模板彙整,口徑與 insights 共用。

守護不變量:counts/rate 與 insights OK/FAIL 口徑一致;delta 以前一等長窗對照,前窗無紀錄
=None;空窗 rate=None 且 markdown 照樣渲染不炸;days 夾 1..30;PR 清單 join 任務標題。
"""

from __future__ import annotations

import json
import time

import pytest

from studio import backlog, config, digest


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    import studio.lessons as lessons_mod

    monkeypatch.setattr(lessons_mod, "_path", lambda: tmp_path / "lessons.json")
    monkeypatch.setattr(lessons_mod, "_read_cache", {}, raising=False)
    return tmp_path


def _write_audit(tmp_path, records):
    (tmp_path / "ap" / "audit.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8"
    )


def test_digest_counts_rate_delta_and_pr_join(tmp_path):
    now = time.time()
    t = backlog.add("修復 X")
    _write_audit(
        tmp_path,
        [
            {"ts": now - 3600, "task_id": t["id"], "outcome": "merged", "pr": 42},
            {"ts": now - 7200, "task_id": 99, "outcome": "merge_failed"},
            {"ts": now - 8 * 86400, "task_id": 98, "outcome": "merged", "pr": 41},  # 前窗
            {"ts": now - 8 * 86400, "task_id": 97, "outcome": "merged", "pr": 40},  # 前窗
        ],
    )
    d = digest.build_digest(days=7)
    assert d["counts"] == {"merged": 1, "merge_failed": 1}
    assert d["completion_rate"] == 0.5
    assert d["prev_completion_rate"] == 1.0
    assert d["delta"] == -0.5
    assert d["prs"][0]["pr"] == 42 and d["prs"][0]["title"] == "修復 X", "PR join 任務標題"

    md = digest.render_markdown(d)
    assert "Ti 週報" in md and "#42 修復 X" in md and "50%" in md


def test_digest_empty_window_renders(tmp_path):
    d = digest.build_digest(days=7)
    assert d["completion_rate"] is None and d["delta"] is None
    md = digest.render_markdown(d)
    assert "Ti 週報" in md and "（無）" in md or "(無)" in md


def test_digest_days_clamped(tmp_path):
    assert digest.build_digest(days=999)["window"]["days"] == 30
    assert digest.build_digest(days=0)["window"]["days"] == 1


def test_digest_prev_window_missing_no_delta(tmp_path):
    now = time.time()
    _write_audit(tmp_path, [{"ts": now - 100, "task_id": 1, "outcome": "merged", "pr": 1}])
    d = digest.build_digest(days=7)
    assert d["completion_rate"] == 1.0
    assert d["prev_completion_rate"] is None and d["delta"] is None, "前窗無紀錄不顯示 delta"
