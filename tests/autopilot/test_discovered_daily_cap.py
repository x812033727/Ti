"""每日自產任務總量閘(第五輪 C2):TI_AUTOPILOT_DISCOVERED_DAILY_CAP。

背景:pending 172 筆中 85% 是系統自產(source=discovered/eval),產生速度 > 消化速度
(吞吐 ~8/天)。既有防線(價值閘/相似度去重/寬度/代數)擋「爛的與同源太多的」,
此閘擋「好但總量太多的」——縱橫之外的總量閘。

守護不變量:
- _discovered_added_today 只計 UTC 當日、source∈{discovered,eval};
- _discovered_budget_left:旋鈕 0=不限;配額內原數放行;超額截斷並記 log;
- _add_discovered_followups 在品質/寬度閘之後套用總量閘。
"""

from __future__ import annotations

import json
import time

import pytest

from studio import autopilot, backlog, config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_DISCOVERED_DAILY_CAP", 5)
    return tmp_path


def _add_discovered(n, *, age_s=0.0, source="discovered"):
    for i in range(n):
        t = backlog.add(f"自產任務 {source} {age_s} {i}", source=source)
        if age_s:
            p = backlog._path(None)
            data = json.loads(p.read_text(encoding="utf-8"))
            for task in data["tasks"]:
                if task["id"] == t["id"]:
                    task["created_at"] = time.time() - age_s
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_counts_only_today_and_self_sources():
    _add_discovered(2)
    _add_discovered(1, source="eval")
    _add_discovered(3, age_s=2 * 86400)  # 前天:不計
    backlog.add("人工任務", source="manual")  # 非自產:不計
    assert autopilot._discovered_added_today() == 3


def test_budget_left_and_knob_zero(caplog):
    import logging

    _add_discovered(4)
    assert autopilot._discovered_budget_left("測試", 1) == 1, "配額內放行"
    with caplog.at_level(logging.INFO, logger="ti.autopilot"):
        assert autopilot._discovered_budget_left("測試", 3) == 1, "超額截斷到剩餘配額"
    assert any("每日自產上限" in r.getMessage() for r in caplog.records), "丟棄必須留痕"
    config.AUTOPILOT_DISCOVERED_DAILY_CAP = 0
    assert autopilot._discovered_budget_left("測試", 99) == 99, "0=不限"


def test_followups_respect_daily_cap(monkeypatch):
    _add_discovered(4)  # 今日已用 4/5
    monkeypatch.setattr(autopilot, "_screen_followups", lambda items, titles: items)
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_MAX_PER_TASK", 0)
    parent = {"id": 1, "gen": 0}
    added = autopilot._add_discovered_followups(
        parent, ["後續甲", "後續乙", "後續丙"], [], structured=False
    )
    assert added == 1, "品質/寬度閘後最後套總量閘:只剩 1 格配額"
    assert autopilot._discovered_added_today() == 5
