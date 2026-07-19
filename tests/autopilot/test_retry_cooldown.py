"""重試冷卻+調查輸出留痕(2026-07-11 09:24 LLM 劣化窗口教訓)。

事故:provider 短暫劣化(SDK 2-4 秒回垃圾)期間,調查失敗退回 pending 後旁路 60s 即
重抓,3 次 attempts 在 3 分鐘內於同一窗口燒光,4 任務冤死 failed;且調查線事件走
_noop 丟棄(history 0 events),缺結論時無從驗屍原始輸出。

守護不變量:
- 討論未收斂退回 pending 時帶 retry_after=now+冷卻(旋鈕 >0);旋鈕 0=不帶(舊行為);
- next_pending/claim_next 跳過 retry_after 在未來的任務,到點自然恢復;
- 欄位缺失/非數值=無冷卻(舊資料不受影響);
- 缺結論時 log.warning 記原始輸出頭段(len+內容)。
"""

from __future__ import annotations

import time

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", True)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_TIMEOUT", 30)
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 3)
    return tmp_path


def test_backlog_skips_cooling_tasks(state):
    hot = backlog.add("冷卻中的任務")
    backlog.set_status(hot["id"], "pending", retry_after=time.time() + 300)
    assert backlog.next_pending() is None, "retry_after 在未來不得被揀"
    assert backlog.claim_next(lambda _t: True) is None, "claim_next 同樣要尊重冷卻"

    backlog.set_status(hot["id"], "pending", retry_after=time.time() - 1)
    assert backlog.next_pending()["id"] == hot["id"], "到點自然恢復可揀"


def test_backlog_legacy_tasks_unaffected(state):
    t = backlog.add("舊資料無 retry_after")
    assert backlog.next_pending()["id"] == t["id"]
    backlog.set_status(t["id"], "pending", retry_after="not-a-number")
    assert backlog.claim_next(lambda _t: True)["id"] == t["id"], "非數值視同無冷卻"


def test_discussion_incomplete_stamps_cooldown(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_RETRY_COOLDOWN_S", 600)
    t = backlog.add("未收斂任務")
    autopilot._handle_discussion_incomplete(dict(t), reason="測試")
    got = next(x for x in backlog.list_tasks("pending") if x["id"] == t["id"])
    assert time.time() + 550 < got["retry_after"] <= time.time() + 600


def test_discussion_incomplete_no_cooldown_when_disabled(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_RETRY_COOLDOWN_S", 0)
    t = backlog.add("未收斂任務")
    autopilot._handle_discussion_incomplete(dict(t), reason="測試")
    got = next(x for x in backlog.list_tasks("pending") if x["id"] == t["id"])
    assert "retry_after" not in got, "旋鈕 0 必須維持舊行為(不帶欄位)"


@pytest.mark.asyncio
async def test_unparseable_output_head_is_logged(state, monkeypatch, caplog):
    import studio.experts as experts_mod

    class _JunkExpert:
        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            return "Execution error: transport closed"

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _JunkExpert)
    monkeypatch.setattr(config, "AUTOPILOT_RETRY_COOLDOWN_S", 0)
    t = backlog.add("調查 X 的根因並回報")
    backlog.set_status(t["id"], "in_progress")
    with caplog.at_level("WARNING", logger="ti.autopilot"):
        await autopilot._run_investigation_task(dict(t), str(state), "apinvtest001", time.time())
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "調查輸出無法解析" in joined and "Execution error: transport closed" in joined, (
        "缺結論時必須留痕原始輸出頭段,否則劣化窗口無從驗屍"
    )
