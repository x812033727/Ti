"""Autopilot 每日 PR 成本熔斷（AUTOPILOT_DAILY_PR_BUDGET）。"""

from __future__ import annotations

import time

import pytest

from studio import autopilot, config

# 固定 struct_time 常數：注意 autopilot.time 即 time 模組本身，monkeypatch gmtime 後
# 不可在替身內再呼叫 time.gmtime（會遞迴），故預先算好兩個不同 UTC 日。
_DAY0 = time.gmtime(0)  # 1970-01-01
_DAY1 = time.gmtime(86400)  # 1970-01-02


@pytest.fixture(autouse=True)
def _reset_budget(monkeypatch):
    # 每個測試從乾淨的行程計數起算，並固定 UTC 日戳避免跨日汙染。
    monkeypatch.setattr(autopilot, "_daily_pr_count", 0)
    monkeypatch.setattr(autopilot, "_daily_pr_day", "")
    monkeypatch.setattr(autopilot, "_daily_pr_notified", False)
    monkeypatch.setattr(autopilot.time, "gmtime", lambda *a: _DAY0)


def _capture_pause(pauses):
    def pause(reason):
        pauses.append(reason)
        return True

    return pause


def test_disabled_budget_has_zero_side_effects(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 0)
    monkeypatch.setattr(autopilot, "_pause", lambda _reason: pytest.fail("disabled must not pause"))
    monkeypatch.setattr(
        autopilot.notify, "send_bg", lambda *a, **k: pytest.fail("disabled must not notify")
    )

    autopilot._check_daily_pr_budget()

    assert autopilot._daily_pr_count == 0


def test_under_budget_does_not_pause(monkeypatch):
    pauses: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 2)
    monkeypatch.setattr(autopilot, "_pause", _capture_pause(pauses))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a, k)))

    autopilot._check_daily_pr_budget()

    assert autopilot._daily_pr_count == 1
    assert pauses == []
    assert sent == []


def test_reaching_budget_pauses_and_notifies_once(monkeypatch):
    pauses: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 2)
    monkeypatch.setattr(autopilot, "_pause", _capture_pause(pauses))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a[0], k)))

    autopilot._check_daily_pr_budget()  # 1：未達
    autopilot._check_daily_pr_budget()  # 2：達門檻 → pause + notify
    autopilot._check_daily_pr_budget()  # 3：同日不重複通知

    assert len(pauses) == 1
    assert len(sent) == 1
    assert sent[0][0] == "daily_pr_budget_pause"
    assert sent[0][1]["daily_pr_count"] == 2
    assert sent[0][1]["budget"] == 2
    assert autopilot._daily_pr_count == 3


def test_reaching_budget_writes_pause_file(monkeypatch, tmp_path):
    sent: list = []
    pause_file = tmp_path / "autopilot.pause"
    monkeypatch.delenv("TI_AUTOPILOT_PAUSED", raising=False)
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 1)
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", pause_file)
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a[0], k)))

    autopilot._check_daily_pr_budget()

    assert config.autopilot_paused() is True
    assert "每日 PR 成本熔斷" in pause_file.read_text(encoding="utf-8")
    assert sent == [
        (
            "daily_pr_budget_pause",
            {"daily_pr_count": 1, "budget": 1, "utc_day": "1970-01-01"},
        )
    ]


def test_cross_utc_day_resets_count_and_notification(monkeypatch):
    pauses: list[str] = []
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 2)
    monkeypatch.setattr(autopilot, "_pause", _capture_pause(pauses))
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a[0], k)))

    # 第 0 天：兩次達門檻 → notify 一次。
    monkeypatch.setattr(autopilot.time, "gmtime", lambda *a: _DAY0)
    autopilot._check_daily_pr_budget()
    autopilot._check_daily_pr_budget()
    assert len(sent) == 1

    # 跨到第 1 天：計數與通知旗標歸零，單次不再 pause。
    monkeypatch.setattr(autopilot.time, "gmtime", lambda *a: _DAY1)
    autopilot._check_daily_pr_budget()

    assert autopilot._daily_pr_count == 1
    assert len(sent) == 1  # 新的一天尚未達門檻，無新通知


def test_pause_failure_does_not_latch_notification(monkeypatch):
    sent: list = []
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 1)
    monkeypatch.setattr(autopilot, "_pause", lambda _reason: False)
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *a, **k: sent.append((a, k)))

    autopilot._check_daily_pr_budget()

    assert sent == []
    assert autopilot._daily_pr_notified is False


def test_config_reload_reads_daily_pr_budget(monkeypatch):
    snapshot = {name: getattr(config, name) for name in dir(config) if name.isupper()}
    try:
        monkeypatch.setenv("TI_AUTOPILOT_DAILY_PR_BUDGET", "9")
        config.reload()
        assert config.AUTOPILOT_DAILY_PR_BUDGET == 9
    finally:
        for name, value in snapshot.items():
            setattr(config, name, value)
