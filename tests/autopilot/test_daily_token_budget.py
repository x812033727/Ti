"""每日 token 成本熔斷（TI_AUTOPILOT_DAILY_TOKEN_BUDGET）守護測試。"""

from __future__ import annotations

import json
import time

import pytest

from studio import autopilot, config, events


class StopLoop(Exception):
    pass


@pytest.fixture(autouse=True)
def _base_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "AUTOPILOT_PAUSED")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_MAX_SLEEP", 1800)
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 0)
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_TOKEN_BUDGET", 0)
    monkeypatch.delenv("TI_AUTOPILOT_PAUSED", raising=False)
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)


def _write_meta(
    sid: str,
    *,
    started_at: float | None = None,
    tokens: int | str | None = 0,
    requirement: str = "[autopilot] 任務",
) -> None:
    config.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    meta = {
        "session_id": sid,
        "requirement": requirement,
        "started_at": time.time() if started_at is None else started_at,
        "status": "completed",
        "token_usage": {"total": {"total": tokens}},
    }
    (config.HISTORY_ROOT / f"{sid}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def test_token_budget_zero_never_exceeded():
    _write_meta("ap1", tokens=999_999)
    assert autopilot._daily_token_budget_exceeded() is False


def test_count_only_today_autopilot_sessions(monkeypatch):
    now = 1_735_692_000.0  # 2025-01-01 01:00:00 UTC
    _write_meta("ap-today", started_at=now - 60, tokens=80)
    _write_meta("user-today", started_at=now - 60, tokens=900, requirement="一般使用者場次")
    _write_meta("ap-yesterday", started_at=now - 7200, tokens=700)
    _write_meta("ap-bad", started_at=now - 60, tokens="not-a-number")

    monkeypatch.setattr(config, "AUTOPILOT_DAILY_TOKEN_BUDGET", 81)
    assert autopilot._todays_autopilot_token_count(now) == 80
    assert autopilot._daily_token_budget_exceeded(now) is False

    monkeypatch.setattr(config, "AUTOPILOT_DAILY_TOKEN_BUDGET", 80)
    assert autopilot._daily_token_budget_exceeded(now) is True


@pytest.mark.asyncio
async def test_main_loop_over_token_budget_sleeps_without_work(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_TOKEN_BUDGET", 100)
    _write_meta("ap-used", tokens=100)
    sleeps: list[float] = []
    touched = {"clone": False, "eval": False, "task": False}

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise StopLoop

    async def fake_prepare_clone():
        touched["clone"] = True
        return "/unused"

    async def fake_evaluate_self(_clone):
        touched["eval"] = True
        return 0

    async def fake_run_one_task(_task):
        touched["task"] = True

    monkeypatch.setattr(autopilot.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "_evaluate_self", fake_evaluate_self)
    monkeypatch.setattr(autopilot, "run_one_task", fake_run_one_task)

    with pytest.raises(StopLoop):
        await autopilot._main_loop(0.0)

    assert sleeps and 60 <= sleeps[0] <= config.AUTOPILOT_QUOTA_MAX_SLEEP
    assert touched == {"clone": False, "eval": False, "task": False}


@pytest.mark.asyncio
async def test_evaluate_self_records_token_usage_for_budget(monkeypatch):
    class FakeExpert:
        def __init__(self, *_args, **_kwargs):
            pass

        async def speak(self, _prompt, broadcast):
            await broadcast(
                events.token_usage(
                    "ap-eval-test",
                    "senior",
                    "claude",
                    "m",
                    7,
                    5,
                    12,
                )
            )
            return "任務: 修正 token 預算測試"

        async def stop(self):
            return None

    import studio.experts as experts_mod

    monkeypatch.setattr(experts_mod, "Expert", FakeExpert)

    added = await autopilot._evaluate_self("/tmp")

    assert added == 1
    assert autopilot._todays_autopilot_token_count() == 12
