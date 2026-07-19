"""第 3 階信任指標(A0 基線):interventions/events 留痕 + trust_metrics 聚合 + digest 呈現。

守護不變量:
- interventions.record:三類白名單;未知 category 歸 output_review(fail-conservative,
  寧可低估零介入率);detail 夾 200;jsonl_log 壞行容錯。
- notify:send/send_bg 無論 webhook 是否設定都落檔 events.jsonl(未設仍零網路);
  record 僅落檔不推播。
- trust_metrics:零介入合併=窗內 merged 且該 task_id 無 output_review 介入;
  first_try=attempts==0;reconciled 計數;事件計數;days 夾 1..90;視窗外排除。
- digest markdown 帶「信任指標」節(口徑同 trust_metrics,單一真相)。
- jsonl_log 壓實:超門檻把保留期外舊紀錄搬 .old,近期紀錄保留。
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from studio import backlog, config, insights, interventions, jsonl_log, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def _write_audit(tmp_path, records):
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    (tmp_path / "ap" / "audit.jsonl").write_text("\n".join(lines), encoding="utf-8")


# --- interventions -----------------------------------------------------------


def test_intervention_record_and_read(tmp_path):
    interventions.record("task_action", "output_review", task_id=7, detail="retry")
    interventions.record("manual_task", "context_feeding", task_id=8)
    interventions.record("pause", "ops")
    recs = interventions.read_window(1)
    assert [r["kind"] for r in recs] == ["task_action", "manual_task", "pause"]
    assert recs[0]["category"] == "output_review" and recs[0]["task_id"] == 7


def test_intervention_unknown_category_fail_conservative(tmp_path):
    interventions.record("weird", "not-a-category", task_id=1, detail="x" * 500)
    rec = interventions.read_window(1)[0]
    assert rec["category"] == "output_review", "未知 category 歸 output_review(寧可低估信任)"
    assert len(rec["detail"]) == 200, "detail 夾長度"


# --- notify events 落檔 ------------------------------------------------------


def test_notify_persists_events_without_webhook(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
    assert notify.send("quota_exhausted", "額度耗盡") is False
    notify.send_bg("loop_stall", "停滯", idle_for=900)
    notify.record("critic_reject", task_id=3, rejects=1)
    assert not called, "未設 webhook 零網路"
    kinds = [e["kind"] for e in notify.read_events(1)]
    assert kinds == ["quota_exhausted", "loop_stall", "critic_reject"], "無條件落檔"
    assert notify.read_events(1)[1]["idle_for"] == 900


# --- trust_metrics -----------------------------------------------------------


def test_trust_metrics_zero_touch_and_events(tmp_path):
    now = time.time()
    _write_audit(
        tmp_path,
        [
            {"ts": now, "task_id": 1, "outcome": "merged", "attempts": 0},
            {"ts": now, "task_id": 2, "outcome": "merged", "attempts": 2},
            {"ts": now, "task_id": 3, "outcome": "merged", "attempts": 0, "reconciled": True},
            {"ts": now, "task_id": 4, "outcome": "merge_failed", "attempts": 1},
            {"ts": now - 100 * 86400, "task_id": 5, "outcome": "merged"},  # 視窗外
        ],
    )
    # task 2 被人工複審(output_review);task 1 只有補背景介入(不影響零介入口徑)
    interventions.record("task_action", "output_review", task_id=2, detail="retry")
    interventions.record("manual_task", "context_feeding", task_id=1)
    interventions.record("pause", "ops")
    notify.record("gate_failure", gate="test", task_id=4)
    notify.record("critic_reject", task_id=2)

    m = insights.trust_metrics(days=7)
    assert m["merged"] == 3 and m["zero_touch"] == 2
    assert m["zero_touch_rate"] == round(2 / 3, 3)
    assert m["first_try_merged"] == 2 and m["reconciled_merges"] == 1
    assert m["interventions"]["total"] == 3
    assert m["interventions"]["by_category"] == {
        "output_review": 1,
        "context_feeding": 1,
        "ops": 1,
    }
    assert m["events"]["gate_failure"] == 1 and m["events"]["critic_reject"] == 1
    assert m["events"]["quota_exhausted"] == 0, "關注 kind 恆出現(計 0)"


def test_trust_metrics_empty_and_clamp(tmp_path):
    m = insights.trust_metrics(days=999)
    assert m["days"] == 90
    assert m["merged"] == 0 and m["zero_touch_rate"] is None
    assert insights.trust_metrics(days=-1)["days"] == 1


# --- digest 呈現 -------------------------------------------------------------


def test_digest_renders_trust_section(tmp_path, monkeypatch):
    import studio.lessons as lessons_mod
    from studio import digest as digest_mod

    monkeypatch.setattr(lessons_mod, "_path", lambda: tmp_path / "lessons.json")
    monkeypatch.setattr(lessons_mod, "_read_cache", {}, raising=False)
    now = time.time()
    _write_audit(tmp_path, [{"ts": now, "task_id": 1, "outcome": "merged", "attempts": 0}])
    interventions.record("manual_task", "context_feeding", task_id=1)
    d = digest_mod.build_digest(7)
    assert d["trust"]["merged"] == 1 and d["trust"]["zero_touch"] == 1
    md = digest_mod.render_markdown(d)
    assert "### 信任指標(第 3 階基線)" in md
    assert "零人工介入合併率:100%" in md
    assert "補背景 1" in md


# --- jsonl_log 壓實 ----------------------------------------------------------


def test_jsonl_log_compaction(tmp_path, monkeypatch):
    path = tmp_path / "ap" / "x.jsonl"
    old_ts = time.time() - 60 * 86400  # 保留期(30 天)外
    jsonl_log.append(path, {"ts": old_ts, "kind": "old"})
    jsonl_log.append(path, {"kind": "fresh"})
    monkeypatch.setattr(jsonl_log, "MAX_BYTES", 1)  # 強制觸發壓實
    jsonl_log.append(path, {"kind": "fresh2"})
    kinds = [r["kind"] for r in jsonl_log.read_window(path, 90)]
    assert kinds == ["fresh", "fresh2"], "保留期外舊紀錄被歸檔"
    archived = (tmp_path / "ap" / "x.jsonl.old").read_text(encoding="utf-8")
    assert '"kind": "old"' in archived
