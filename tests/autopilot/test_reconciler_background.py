"""reconciler 常駐背景線(第五輪 P1)+靜默失能修復的守護。

事故背景(2026-07-11):reconciler 原本只在任務邊界跑,且節流起點=行程啟動時刻——
邊界 execv 搶在 reconcile 之前重載、新行程第一個邊界又被節流擋掉、下一任務跑數小時
無邊界 → 整晚零收斂,3 筆 merging 卡 2-8 小時(PR 其實早已合併)。

守護不變量:
- 節流起點必須是 0.0(行程啟動後第一次檢查就真的跑);間隔改由
  config.AUTOPILOT_RECONCILE_INTERVAL_S 控制,0=停用。
- _reconciler_loop 每輪呼叫 _maybe_reconcile_open_prs;單輪例外不弄死背景線。
- 可觀測:查 PR 失敗記 warning(原 debug=journal 隱形);pass 有 merging 時記 INFO 摘要。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import pytest

from studio import autopilot, backlog, config, publisher


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_AUTO_MERGE", True)
    monkeypatch.setattr(config, "AUTOPILOT_RECONCILE_INTERVAL_S", 300)
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "o/r")
    monkeypatch.setattr(autopilot, "_last_reconcile_at", 0.0)
    monkeypatch.setattr(publisher, "set_repo_override", lambda repo: object())
    monkeypatch.setattr(publisher, "reset_repo_override", lambda token: None)
    return tmp_path


def _merging_task(pr=42):
    t = backlog.add("背景合併中的任務")
    backlog.set_status(t["id"], "merging", pr=pr, merge_armed_at=time.time())
    return t


def _install_run(monkeypatch, result):
    calls: list[list[str]] = []

    async def fake_run(cmd, cwd=None, timeout=600, **kwargs):
        calls.append(list(cmd))
        return result

    monkeypatch.setattr(autopilot, "_run", fake_run)
    return calls


# --- 節流起點與旋鈕 --------------------------------------------------------------


def test_throttle_epoch_is_zero_in_source():
    """回歸守門:節流起點必須寫死 0.0——起點=time.time() 正是整晚失能的根因之一
    (重啟/execv 重新起算+邊界才跑=永遠追不上)。"""
    src = Path(autopilot.__file__).read_text(encoding="utf-8")
    assert re.search(r"^_last_reconcile_at = 0\.0$", src, re.M), (
        "_last_reconcile_at 模組初值必須是 0.0"
    )


@pytest.mark.asyncio
async def test_interval_knob_zero_disables(monkeypatch, state):
    monkeypatch.setattr(config, "AUTOPILOT_RECONCILE_INTERVAL_S", 0)
    _merging_task()
    calls = _install_run(monkeypatch, (0, json.dumps({"state": "MERGED"})))
    await autopilot._maybe_reconcile_open_prs()
    assert not calls, "間隔=0 必須完全停用(邊界+背景共用同一入口)"


@pytest.mark.asyncio
async def test_interval_knob_controls_throttle(monkeypatch, state):
    _merging_task()
    calls = _install_run(monkeypatch, (0, json.dumps({"state": "MERGED"})))
    await autopilot._maybe_reconcile_open_prs()
    n = len(calls)
    assert n, "起點 0.0:行程內第一次檢查就要跑"
    monkeypatch.setattr(autopilot, "_last_reconcile_at", time.time() - 301)
    await autopilot._maybe_reconcile_open_prs()
    assert len(calls) > n, "超過間隔要再跑"


# --- 背景線 ---------------------------------------------------------------------


async def _run_loop_ticks(monkeypatch, ticks_total):
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > ticks_total:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await autopilot._reconciler_loop()


@pytest.mark.asyncio
async def test_background_loop_calls_maybe_each_tick(monkeypatch, state):
    ran = {"n": 0}

    async def fake_maybe():
        ran["n"] += 1

    monkeypatch.setattr(autopilot, "_maybe_reconcile_open_prs", fake_maybe)
    await _run_loop_ticks(monkeypatch, ticks_total=2)
    assert ran["n"] == 2, "每輪醒來都要呼叫(間隔判斷在 _maybe 內)"


@pytest.mark.asyncio
async def test_background_loop_survives_exception(monkeypatch, state):
    ran = {"n": 0}

    async def boom():
        ran["n"] += 1
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(autopilot, "_maybe_reconcile_open_prs", boom)
    await _run_loop_ticks(monkeypatch, ticks_total=2)
    assert ran["n"] == 2, "單輪例外不得弄死背景線"


# --- 可觀測性 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gh_view_failure_logs_warning(monkeypatch, state, caplog):
    t = _merging_task()
    _install_run(monkeypatch, (1, "api error"))
    with caplog.at_level(logging.WARNING, logger="ti.autopilot"):
        await autopilot._maybe_reconcile_open_prs()
    assert any("查 PR #42 失敗" in r.getMessage() for r in caplog.records), (
        "gh 失敗必須 warning 可見——整個 pass 靜默失能曾以零 log 呈現"
    )
    assert next(x for x in backlog.list_tasks() if x["id"] == t["id"])["status"] == "merging"


@pytest.mark.asyncio
async def test_pass_summary_logged(monkeypatch, state, caplog):
    _merging_task()
    monkeypatch.setattr(autopilot, "_append_audit", lambda rec: None)
    _install_run(
        monkeypatch,
        (0, json.dumps({"state": "MERGED", "mergeStateStatus": "", "statusCheckRollup": []})),
    )
    with caplog.at_level(logging.INFO, logger="ti.autopilot"):
        await autopilot._maybe_reconcile_open_prs()
    assert any("核對 1 筆 merging" in r.getMessage() for r in caplog.records), (
        "pass 級摘要:journal 必須能分辨 reconciler 有沒有在跑"
    )
