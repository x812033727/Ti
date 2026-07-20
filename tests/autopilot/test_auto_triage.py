"""主迴圈自動 failed 分診（_maybe_triage_failed）：頻率護欄、容錯、只在有動作時記 log。

背景（backlog #279）：backlog.triage_failed（確定性、無 LLM）原本只掛在
POST /api/autopilot/triage 手動端點——基礎設施型失敗（provider 掛掉／429／網路）的
任務永遠躺在 failed，要人工按按鈕才復活。主迴圈每輪呼叫 _maybe_triage_failed，
以 _TRIAGE_INTERVAL_S（15 分鐘）護欄防止每輪全量掃 backlog 的多餘 IO。
"""

from __future__ import annotations

import pytest

from studio import autopilot


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    """每個測試從「距上次分診已久」的狀態出發，測試間互不污染。"""
    monkeypatch.setattr(autopilot, "_last_triage_at", 0.0)


def test_triage_runs_and_guard_blocks_second_call(monkeypatch):
    """首次呼叫真的跑分診；護欄內的第二次呼叫直接跳過。"""
    calls: list[int] = []
    monkeypatch.setattr(
        autopilot.backlog, "triage_failed", lambda: calls.append(1) or {"retried": 1, "parked": 0}
    )
    autopilot._maybe_triage_failed()
    autopilot._maybe_triage_failed()
    assert len(calls) == 1


def test_triage_runs_again_after_interval(monkeypatch):
    """超過 _TRIAGE_INTERVAL_S 後再呼叫，會再跑一次。"""
    calls: list[int] = []
    monkeypatch.setattr(
        autopilot.backlog, "triage_failed", lambda: calls.append(1) or {"retried": 0, "parked": 0}
    )
    autopilot._maybe_triage_failed()
    # 把「上次分診時間」撥回護欄之外，模擬時間流逝
    autopilot._last_triage_at -= autopilot._TRIAGE_INTERVAL_S + 1
    autopilot._maybe_triage_failed()
    assert len(calls) == 2


def test_triage_exception_never_propagates(monkeypatch):
    """分診炸掉只記 log，絕不影響主迴圈；且護欄已推進（壞掉時不會每輪重試狂轟）。"""

    def _boom():
        raise RuntimeError("backlog 檔案壞掉")

    monkeypatch.setattr(autopilot.backlog, "triage_failed", _boom)
    autopilot._maybe_triage_failed()  # 不得 raise
    assert autopilot._last_triage_at > 0.0


def test_triage_logs_only_when_action_taken(monkeypatch, caplog):
    """零動作（retried=parked=0）不記 info log，避免每 15 分鐘一行噪音。"""
    monkeypatch.setattr(autopilot.backlog, "triage_failed", lambda: {"retried": 0, "parked": 0})
    with caplog.at_level("INFO", logger="ti.autopilot"):
        autopilot._maybe_triage_failed()
    assert "failed 自動分診" not in caplog.text

    autopilot._last_triage_at -= autopilot._TRIAGE_INTERVAL_S + 1
    monkeypatch.setattr(autopilot.backlog, "triage_failed", lambda: {"retried": 2, "parked": 4})
    with caplog.at_level("INFO", logger="ti.autopilot"):
        autopilot._maybe_triage_failed()
    assert "failed 自動分診" in caplog.text
