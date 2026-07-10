"""backlog failed 分診（triage_failed）的單元測試：確定性規則、無 LLM。

涵蓋：基礎設施型失敗退回 pending（正反案）、attempts 上限擋重試、單次 10 筆上限、
陳年失敗歸檔 parked（14 天門檻）、legacy status "cancelled" 洗白、parked 不被
next_pending 撿走、counts 含 parked 鍵。
"""

from __future__ import annotations

import json
import time

import pytest

from studio import backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_TASK_MAX_ATTEMPTS", 3)
    return tmp_path


def _fail(title: str, note: str, *, attempts: int = 1, age_s: float = 0) -> dict:
    """造一筆 failed 任務：可指定 note/attempts，並把 updated_at 回撥 age_s 秒。"""
    t = backlog.add(title)
    backlog.set_status(t["id"], "failed", note=note, attempts=attempts)
    if age_s:
        _patch_task(t["id"], updated_at=time.time() - age_s)
    return t


def _patch_task(task_id: int, **fields) -> None:
    """直接改 backlog.json 的任務欄位（模擬歷史資料，繞過 set_status 的合法性檢查）。"""
    p = backlog._path(None)
    data = json.loads(p.read_text(encoding="utf-8"))
    for t in data["tasks"]:
        if t["id"] == task_id:
            t.update(fields)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _get(task_id: int) -> dict:
    return next(t for t in backlog.list_tasks() if t["id"] == task_id)


# --- 規則 1：基礎設施型失敗 → 退回 pending 重試 ----------------------------


@pytest.mark.parametrize(
    "note",
    [
        "(逾時 600s)",
        "TimeoutError: autopilot task timeout after 3600s",
        "官方額度端點 unreachable",
        "claude provider unavailable",
        "重佈失敗（health check 未過）→ 回滾成功",
        "另一個部署進行中，略過本輪",
    ],
)
def test_infra_failures_are_retried(state, note):
    t = _fail("基礎設施型失敗", note)
    stats = backlog.triage_failed()
    assert stats == {"retried": 1, "parked": 0, "revived": 0}
    cur = _get(t["id"])
    assert cur["status"] == "pending"
    assert cur["attempts"] == 0  # attempts 重置
    assert "[triage]" in cur["note"]


@pytest.mark.parametrize(
    "note",
    [
        "討論未達完成",
        "[test] 連續 3 次未過，放棄；FAILED tests/test_x.py",
        "ValueError: 任務本身缺陷",
    ],
)
def test_non_infra_failures_are_not_retried(state, note):
    t = _fail("任務本身缺陷", note)
    stats = backlog.triage_failed()
    # 未滿 14 天 → 不歸檔；「討論未達完成」未滿 24h 冷卻 → 也不復活
    assert stats == {"retried": 0, "parked": 0, "revived": 0}
    assert _get(t["id"])["status"] == "failed"


def test_infra_failure_at_attempts_cap_is_not_retried(state):
    """note 命中基礎設施 regex 但 attempts 已達上限 → 不重試（避免無限重試迴圈）。"""
    t = _fail("逾時但額度用罄", "(逾時 600s)", attempts=3)
    stats = backlog.triage_failed()
    assert stats["retried"] == 0
    assert _get(t["id"])["status"] == "failed"


def test_retry_capped_at_ten_most_recent(state):
    """單次分診至多退回 10 筆（取 updated_at 最近者），其餘維持 failed。"""
    ids = []
    for i in range(12):
        t = _fail(f"逾時任務 {i}", "(逾時 600s)", age_s=(12 - i) * 60)
        ids.append(t["id"])
    stats = backlog.triage_failed()
    assert stats["retried"] == backlog.TRIAGE_RETRY_MAX == 10
    statuses = {tid: _get(tid)["status"] for tid in ids}
    assert sum(1 for s in statuses.values() if s == "pending") == 10
    # 最舊的兩筆（回撥最久）留在 failed
    assert statuses[ids[0]] == "failed" and statuses[ids[1]] == "failed"


# --- 規則 2：陳年失敗 → parked ---------------------------------------------


def test_stale_non_infra_failure_is_parked(state):
    t = _fail("放棄的任務", "[test] 連續 3 次未過，放棄", attempts=3, age_s=15 * 86400)
    stats = backlog.triage_failed()
    assert stats == {"retried": 0, "parked": 1, "revived": 0}
    assert _get(t["id"])["status"] == "parked"


def test_stale_discussion_incomplete_is_parked(state):
    t = _fail("討論沒完成", "討論未達完成", age_s=14 * 86400 + 60)
    assert backlog.triage_failed()["parked"] == 1
    assert _get(t["id"])["status"] == "parked"


def test_fresh_failure_stays_failed(state):
    t = _fail("剛失敗", "討論未達完成", age_s=3600)  # 1 小時 < 24h 冷卻 < 14 天
    assert backlog.triage_failed() == {"retried": 0, "parked": 0, "revived": 0}
    assert _get(t["id"])["status"] == "failed"


def test_stale_infra_failure_over_attempts_is_parked(state):
    """基礎設施 note 但 attempts 達上限：不重試；滿 14 天則走歸檔分支。"""
    t = _fail("重試耗盡的逾時", "(逾時 600s)", attempts=3, age_s=20 * 86400)
    stats = backlog.triage_failed()
    assert stats == {"retried": 0, "parked": 1, "revived": 0}
    assert _get(t["id"])["status"] == "parked"


# --- 規則 3：legacy "cancelled" 洗白 ----------------------------------------


def test_legacy_cancelled_is_parked(state):
    t = backlog.add("歷史殘留")
    # set_status 會 reject 非法值（守住既有合約），故 legacy 值只能由 triage 在鎖內洗白。
    with pytest.raises(ValueError):
        backlog.set_status(t["id"], "cancelled")
    _patch_task(t["id"], status="cancelled")
    stats = backlog.triage_failed()
    assert stats["parked"] == 1
    cur = _get(t["id"])
    assert cur["status"] == "parked"
    assert "cancelled" in cur["note"]


# --- parked 的隔離性 ---------------------------------------------------------


def test_parked_not_picked_by_next_pending(state):
    t = _fail("陳年失敗", "討論未達完成", age_s=15 * 86400)
    backlog.triage_failed()
    assert _get(t["id"])["status"] == "parked"
    assert backlog.next_pending() is None  # parked 不會被撿走
    fresh = backlog.add("新任務")
    assert backlog.next_pending()["id"] == fresh["id"]


def test_counts_includes_parked_key(state):
    assert backlog.counts()["parked"] == 0
    _fail("陳年失敗", "討論未達完成", age_s=15 * 86400)
    backlog.triage_failed()
    assert backlog.counts()["parked"] == 1


def test_triage_empty_backlog_is_noop(state):
    assert backlog.triage_failed() == {"retried": 0, "parked": 0, "revived": 0}


# --- 規則 2b：討論未收斂冷卻復活（第五輪 C1）--------------------------------


def test_discussion_incomplete_revived_once_after_cooldown(state):
    t = _fail("討論沒完成", "討論未達完成（連續 3 次未收斂，放棄）", attempts=3, age_s=2 * 86400)
    stats = backlog.triage_failed()
    assert stats == {"retried": 0, "parked": 0, "revived": 1}
    cur = _get(t["id"])
    assert cur["status"] == "pending"
    assert cur["attempts"] == 0, "復活附帶 attempts 歸零，才有完整一輪重試空間"
    assert cur["discussion_revives"] == 1
    assert "冷卻復活" in cur["note"]


def test_discussion_incomplete_not_revived_twice(state):
    """復活過一次（discussion_revives=1）再失敗 → 不再復活，等 14 天歸檔。"""
    t = _fail("又沒收斂", "討論未達完成（連續 3 次未收斂，放棄）", age_s=2 * 86400)
    _patch_task(t["id"], discussion_revives=1)
    assert backlog.triage_failed()["revived"] == 0
    assert _get(t["id"])["status"] == "failed"


def test_stale_discussion_incomplete_parks_not_revives(state):
    """已滿 14 天的陳年討論失敗直接歸檔，不再折騰復活。"""
    t = _fail("陳年討論失敗", "討論未達完成", age_s=15 * 86400)
    stats = backlog.triage_failed()
    assert stats == {"retried": 0, "parked": 1, "revived": 0}
    assert _get(t["id"])["status"] == "parked"


def test_c1_config_defaults():
    """第五輪 C1 預設值守門：討論重試與閘門對齊、BEHIND 追趕輪放寬（conftest 已清 TI_* env）。"""
    src_defaults = (config.AUTOPILOT_DISCUSSION_MAX_ATTEMPTS, config.MERGE_BEHIND_RETRIES)
    assert src_defaults == (3, 4)


def test_revive_shares_retry_budget(state):
    """復活與 infra 重試共用 TRIAGE_RETRY_MAX 配額：infra 佔滿即不復活。"""
    for i in range(10):
        _fail(f"逾時 {i}", "(逾時 600s)")
    t = _fail("討論沒完成", "討論未達完成", age_s=2 * 86400)
    stats = backlog.triage_failed()
    assert stats["retried"] == 10 and stats["revived"] == 0
    assert _get(t["id"])["status"] == "failed", "配額用罄留待下輪"
