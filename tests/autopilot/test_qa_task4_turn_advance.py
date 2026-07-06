"""守門測試 #4：current_expert / turn_started_at 隨專家 turn 正確前進。

驅動 production 的 `autopilot._refresh_status_for_event`（run_one_task 的 broadcast tap），
以受控時鐘與真 status.json 讀寫，驗證：
  - 新 speaker 的 tool_use / final expert_message → 前進 current_expert 並重置 turn_started_at；
  - 同一專家連續事件不重開 turn（turn_started_at 不倒退／不重置）；
  - streaming 未完成（final=False）的 expert_message 不動 turn（避免逐塊事件抖動）；
  - 事件驅動寫入會 preserve 主迴圈既有的 quota（不因 tap 觸發把用量閃成空）。

紅樣本實證（交付前手動破壞）：把 `_refresh_status_for_event` 的
``new_turn = speaker is not None and speaker != current`` 改成 ``speaker is not None``
（＝同一專家每個事件都重開 turn），跑本檔——``test_same_speaker_does_not_reopen_turn`` 立即紅：
``AssertionError: 同一專家不得重置 turn_started_at``。證明「不重開 turn」這條真的被測到。
"""

from __future__ import annotations

import json

import pytest

from studio import autopilot, config, events as events_mod


class _Clock:
    """單調可控時鐘：每次 .time() 回傳當前值；tick() 手動推進，讓 turn 邊界斷言不受真牆鐘與節流抖動影響。"""

    def __init__(self, start: float = 1_783_140_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def tick(self, dt: float) -> float:
        self.now += dt
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clk = _Clock()

    class _FakeTime:
        time = staticmethod(clk.time)

    monkeypatch.setattr(autopilot, "time", _FakeTime)
    return clk


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    # 預置主迴圈寫過的 running 心跳（含 quota），供 preserve 斷言。
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "task_id": 7,
                "sleep_until": None,
                "updated_at": 1_783_139_000.0,
                "quota": {"claude": 12, "codex": 88},
                "last_activity_at": None,
                "workers": None,
                "current_expert": None,
                "turn_started_at": None,
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _read(state_dir) -> dict:
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


def _tool(speaker: str):
    return events_mod.tool_use("s1", speaker, "bash", "run pytest")


def _msg(speaker: str, *, final: bool, streaming: bool = False):
    return events_mod.expert_message(
        "s1", speaker, speaker, "🧑", "…", streaming=streaming, final=final
    )


def test_turn_advances_across_speakers(clock, state_dir):
    """兩位專家依序發言：current_expert 前進、turn_started_at 各自為新 turn 起點。"""
    ts = {"current_expert": None, "turn_started_at": None, "last_status_write_at": None}

    # 第一位 senior 的 tool_use → 開 turn。
    t0 = clock.now
    autopilot._refresh_status_for_event(7, _tool("senior"), ts)
    st = _read(state_dir)
    assert st["current_expert"] == "senior"
    assert st["turn_started_at"] == t0
    assert st["last_activity_at"] == t0
    # preserve：主迴圈的 quota 沒被 tap 閃成空。
    assert st["quota"] == {"claude": 12, "codex": 88}

    # 時間前進，換 qa → 新 turn 起點應為新的 now，不是舊 t0。
    t1 = clock.tick(30.0)
    autopilot._refresh_status_for_event(7, _msg("qa", final=True), ts)
    st = _read(state_dir)
    assert st["current_expert"] == "qa"
    assert st["turn_started_at"] == t1
    assert st["turn_started_at"] != t0


def test_same_speaker_does_not_reopen_turn(clock, state_dir):
    """同一專家連續事件不重開 turn：current_expert 不變、turn_started_at 維持首次起點。"""
    ts = {"current_expert": None, "turn_started_at": None, "last_status_write_at": None}

    t0 = clock.now
    autopilot._refresh_status_for_event(7, _tool("senior"), ts)
    assert _read(state_dir)["turn_started_at"] == t0

    # 同一 senior 後續事件；跳過節流窗（>1s）才會落盤，但無論是否落盤都不得重置 turn。
    clock.tick(5.0)
    autopilot._refresh_status_for_event(7, _msg("senior", final=True), ts)

    st = _read(state_dir)
    assert st["current_expert"] == "senior"
    assert st["turn_started_at"] == t0, "同一專家不得重置 turn_started_at"
    assert ts["turn_started_at"] == t0


def test_streaming_nonfinal_message_ignored(clock, state_dir):
    """streaming 未完成（final=False）的 expert_message 不觸發 turn 更新（規避逐塊事件抖動）。"""
    ts = {"current_expert": None, "turn_started_at": None, "last_status_write_at": None}

    autopilot._refresh_status_for_event(7, _msg("senior", final=False, streaming=True), ts)

    # turn_state 未動、status.json 的 turn 欄位仍是預置的 None。
    assert ts["current_expert"] is None
    assert ts["turn_started_at"] is None
    st = _read(state_dir)
    assert st["current_expert"] is None
    assert st["turn_started_at"] is None


def test_final_streaming_message_opens_turn(clock, state_dir):
    """streaming 但 final=True（串流收尾）視為發言完成 → 開 turn。"""
    ts = {"current_expert": None, "turn_started_at": None, "last_status_write_at": None}

    t0 = clock.now
    autopilot._refresh_status_for_event(7, _msg("senior", final=True, streaming=True), ts)

    st = _read(state_dir)
    assert st["current_expert"] == "senior"
    assert st["turn_started_at"] == t0


def test_non_turn_events_ignored(clock, state_dir):
    """非 tool_use / expert_message 的事件（如 phase_change）不動 turn。"""
    ts = {"current_expert": None, "turn_started_at": None, "last_status_write_at": None}

    autopilot._refresh_status_for_event(7, events_mod.phase_change("s1", "demo", ""), ts)

    assert ts["current_expert"] is None
    assert _read(state_dir)["current_expert"] is None
