"""例外收件匣(軌 F1):澄清票/停放/page 級事件聚合。"""

from __future__ import annotations

import pytest

from studio import backlog, config, insights, jsonl_log


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return tmp_path


def test_empty_state():
    out = insights.attention()
    assert out == {
        "clarify": [],
        "policy_blocked": [],
        "parked": [],
        "events": [],
        "pending_clarify": 0,
        "deploy": None,
    }


def test_policy_blocked_split_and_timeless():
    """政策攔下=等人裁決,與澄清票同語意不看時間(7-21 實錄:陳年後從收件匣隱形)。"""
    import time as _t

    t1 = backlog.add("治理檢討", "", source="manual")
    t2 = backlog.add("普通停放", "", source="eval")
    backlog.set_status(
        t1["id"], "parked", note="自治政策在 deploy 前拒絕：all_verdicts_must_approve"
    )
    backlog.set_status(t2["id"], "parked", note="等外部依賴")
    stale = _t.time() - 40 * 86400
    backlog.set_status(t1["id"], "parked", updated_at=stale)
    out = insights.attention(days=7)
    assert [r["id"] for r in out["policy_blocked"]] == [t1["id"]], "陳年政策攔下仍在"
    assert [r["id"] for r in out["parked"]] == [t2["id"]], "一般停放不混入"
    assert out["pending_clarify"] == 0, "政策攔下不冒充澄清票"


def test_deploy_drift_card(tmp_path):
    """autodeploy 延後檔存在 → 收件匣帶部署漂移卡;壞檔/缺欄回 None 不炸。"""
    import json as _json

    path = config.AUTOPILOT_STATE_DIR / "autodeploy-deferred.json"
    path.write_text(
        _json.dumps(
            {
                "remote": "da1646d6138e729b8cb522d8486d584c186ff04c",
                "reason": "governance_evidence_required",
                "deferrals": 42,
                "first_deferred_at": 1784000000.0,
            }
        ),
        encoding="utf-8",
    )
    out = insights.attention()
    assert out["deploy"] == {
        "remote": "da1646d6138e",
        "reason": "governance_evidence_required",
        "deferrals": 42,
        "first_deferred_at": 1784000000.0,
    }

    path.write_text("{broken", encoding="utf-8")
    assert insights.attention()["deploy"] is None

    path.write_text(_json.dumps({"deferrals": "many"}), encoding="utf-8")
    assert insights.attention()["deploy"] is None, "缺 remote 視為無卡"


def test_stale_parked_excluded_but_stale_clarify_kept():
    """陳年停放=歸檔語意不進收件匣;澄清票沒答就是欠著,不看時間。"""
    import time as _t

    t_old = backlog.add("陳年歸檔", "", source="eval")
    t_oldq = backlog.add("陳年澄清", "", source="manual")
    backlog.set_status(t_old["id"], "parked", note="老東西")
    backlog.set_status(t_oldq["id"], "parked", note="[待澄清] 舊問題", clarify="舊問題?")
    stale = _t.time() - 40 * 86400
    # set_status 的 **fields 在刷新 updated_at 之後 update → 可覆寫成陳年;
    # 澄清票要連 clarify 一起帶(不帶 clarify 的 parked 轉換會清殘留=不變量)。
    backlog.set_status(t_old["id"], "parked", updated_at=stale)
    backlog.set_status(t_oldq["id"], "parked", updated_at=stale, clarify="舊問題?")
    out = insights.attention(days=7)
    assert out["parked"] == [], "陳年停放不進收件匣"
    assert [r["id"] for r in out["clarify"]] == [t_oldq["id"]], "陳年澄清票仍在"


def test_clarify_vs_plain_parked_split():
    t1 = backlog.add("歧義任務", "", source="manual")
    t2 = backlog.add("普通停放", "", source="eval")
    backlog.set_status(t1["id"], "parked", note="[待澄清] 要哪個環境?", clarify="要哪個環境?")
    backlog.set_status(t2["id"], "parked", note="等外部依賴")
    out = insights.attention()
    assert out["pending_clarify"] == 1
    assert [r["id"] for r in out["clarify"]] == [t1["id"]]
    assert out["clarify"][0]["clarify"] == "要哪個環境?"
    assert [r["id"] for r in out["parked"]] == [t2["id"]]
    assert out["parked"][0]["note"] == "等外部依賴"


def test_clarify_invariant_cleared_on_plain_park_kept_on_unpark():
    """clarify 只在帶問題停放時有效:答過→取回→日後無關停放,不得死灰復燃(覆審修)。"""
    t = backlog.add("歧義任務", "", source="manual")
    backlog.set_status(t["id"], "parked", note="[待澄清] 哪個環境?", clarify="哪個環境?")
    # 取回(答覆)保留 clarify——下次執行要注入問題+人工回覆
    task, err = backlog.apply_action(t["id"], "unpark", note="staging")
    assert err == "" and task["clarify"] == "哪個環境?"
    # 之後無關原因停放(不帶新 clarify)→ 殘留問題清掉,不再是澄清票
    backlog.set_status(t["id"], "parked", note="[調查] 其他原因")
    out = insights.attention()
    assert out["pending_clarify"] == 0 and out["clarify"] == []
    assert [r["id"] for r in out["parked"]] == [t["id"]]
    # 手動歸檔同樣清 clarify
    backlog.set_status(t["id"], "parked", note="q", clarify="新問題?")
    backlog.apply_action(t["id"], "unpark")
    task, _ = backlog.apply_action(t["id"], "park")
    assert "clarify" not in task
    # 帶著新問題停放=合法澄清票,保留
    backlog.set_status(t["id"], "parked", note="[待澄清] again", clarify="again?")
    assert insights.attention()["pending_clarify"] == 1


def test_events_page_only_and_noise_excluded():
    path = config.AUTOPILOT_STATE_DIR / "events.jsonl"
    jsonl_log.append(path, {"kind": "task_failed", "title": "任務失敗", "task_id": 9})
    jsonl_log.append(path, {"kind": "critic_reject", "title": "digest 級不進收件匣"})
    jsonl_log.append(path, {"kind": "test", "title": "自證雜訊排除"})
    jsonl_log.append(path, {"kind": "daily_digest", "title": "日報排除"})
    out = insights.attention()
    assert [e["kind"] for e in out["events"]] == ["task_failed"]
    assert out["events"][0]["task_id"] == 9


def test_days_clamped_and_sorted_desc(monkeypatch):
    import time

    now = time.time()
    path = config.AUTOPILOT_STATE_DIR / "events.jsonl"
    jsonl_log.append(path, {"kind": "task_failed", "title": "舊", "ts": now - 100})
    jsonl_log.append(path, {"kind": "loop_stall", "title": "新", "ts": now - 50})
    seen = {}
    real = insights.notify.read_events

    def spy(days, *, state_dir=None):
        seen["days"] = days
        return real(days, state_dir=state_dir)

    monkeypatch.setattr(insights.notify, "read_events", spy)
    out = insights.attention(days=999)
    assert seen["days"] == 30, "days 夾 1..30"
    assert [e["kind"] for e in out["events"]] == ["loop_stall", "task_failed"]
