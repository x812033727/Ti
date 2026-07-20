"""看板洞察聚合(功能強化 D1):audit 趨勢/調查清單 + lessons source bug 修復回歸。

守護不變量:
- audit_trend:UTC 日聚合、壞行跳過、rate 口徑(OK/FAIL 桶;merge_pending 等中性 outcome
  進 outcomes 明細不進分母)、days 夾 1..90、視窗外紀錄排除。
- investigations:note 前綴擷取 + audit join(同 task 取最新)、由新到舊、limit 夾值。
- lessons._VALID_SOURCES 含 "investigation"(2026-07-10 bug:白名單漏列→ValueError 被
  suppress 吞掉,調查結論從未真正入庫)。
"""

from __future__ import annotations

import json
import time

import pytest

from studio import backlog, config, insights, lessons


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def _write_audit(tmp_path, records):
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    lines.insert(1, "{broken json")  # 壞行容錯
    (tmp_path / "ap" / "audit.jsonl").write_text("\n".join(lines), encoding="utf-8")


def test_audit_trend_buckets_and_rate(tmp_path):
    now = time.time()
    _write_audit(
        tmp_path,
        [
            {"ts": now, "task_id": 1, "outcome": "merged"},
            {"ts": now, "task_id": 2, "outcome": "merge_failed"},
            {"ts": now, "task_id": 3, "outcome": "merge_pending"},  # 中性:不進分母
            {"ts": now - 86400, "task_id": 4, "outcome": "investigation_done"},
            {"ts": now - 100 * 86400, "task_id": 5, "outcome": "merged"},  # 視窗外
        ],
    )
    out = insights.audit_trend(days=30)
    assert len(out["buckets"]) == 2, "只含視窗內有紀錄的日"
    today = out["buckets"][-1]
    assert today["ok"] == 1 and today["fail"] == 1 and today["rate"] == 0.5
    assert today["outcomes"]["merge_pending"] == 1, "中性 outcome 進明細"
    assert out["totals"] == {"ok": 2, "fail": 1, "rate": round(2 / 3, 3)}


def test_audit_trend_clamps_and_empty(tmp_path):
    assert insights.audit_trend(days=999)["days"] == 90
    assert insights.audit_trend(days=-1)["days"] == 1
    out = insights.audit_trend()
    assert out["buckets"] == [] and out["totals"]["rate"] is None


def test_investigations_join_and_order(tmp_path):
    t1 = backlog.add("調查 A")
    backlog.set_status(t1["id"], "done", note="[調查結論] 根因是 X")
    t2 = backlog.add("調查 B")
    backlog.set_status(t2["id"], "parked", note="[調查] 需人工:換 token")
    t3 = backlog.add("普通任務")
    backlog.set_status(t3["id"], "done", note="一般完成")
    _write_audit(
        tmp_path,
        [
            {"ts": 1.0, "task_id": t1["id"], "outcome": "investigation_done", "duration_s": 89},
            {"ts": 2.0, "task_id": t1["id"], "outcome": "investigation_done", "duration_s": 120},
        ],
    )
    out = insights.investigations()
    ids = [x["task_id"] for x in out]
    assert t1["id"] in ids and t2["id"] in ids and t3["id"] not in ids
    a = next(x for x in out if x["task_id"] == t1["id"])
    assert a["duration_s"] == 120, "同 task 取最新 audit"
    assert out[0]["updated_at"] >= out[-1]["updated_at"], "由新到舊"


def test_lessons_accepts_investigation_source(tmp_path, monkeypatch):
    """bug 修復回歸:source="investigation" 必須可入庫(先前 ValueError 被 suppress 吞掉)。"""
    monkeypatch.setattr(lessons, "_path", lambda: tmp_path / "lessons.json")
    monkeypatch.setattr(lessons, "_read_cache", {}, raising=False)
    n = lessons.add_many(
        ["調查結論(X):根因是 Y"], session_id="s", requirement="r", source="investigation"
    )
    assert n == 1
    assert any(it.get("source") == "investigation" for it in lessons.all_lessons())
