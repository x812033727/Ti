"""Autopilot：尾票不拖垮整場——帶已知限制出貨(shippable)續走客觀閘門，不在 completed 檢查硬判 failed。

對齊 orchestrator 的「完整完成(done) vs 可出貨(shippable)」分流：單一子任務 known-limit、其餘
N-1/N 已過且核心客觀證據通過時，不該整場記「討論未達完成」failed，而應續走 lint/collect/test/merge
客觀閘門，通過則以已知限制版本合併（done，帶註記）。完全不可出貨才維持 failed。
"""

from __future__ import annotations

import pytest

from studio import autopilot, config


def _common_mocks(monkeypatch, tmp_path, result, statuses, *, gates_ok=True, gate_calls=None):
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _requirement):
            return result

    async def fake_gate(*_args, **_kwargs):
        if gate_calls is not None:
            gate_calls.append(True)
        return (gates_ok, "" if gates_ok else "紅點")

    async def fake_merge(*_args, **_kwargs):
        return (True, "已 squash-merge 進 main")

    async def fake_idle():
        return False  # 略過重佈，聚焦狀態判定

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", FakeSession)
    monkeypatch.setattr(autopilot.history, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "finish_session", lambda *a, **k: None)
    monkeypatch.setattr(
        autopilot.backlog,
        "set_status",
        lambda task_id, status, **kw: statuses.append((task_id, status, kw)),
    )
    monkeypatch.setattr(autopilot.backlog, "add_items", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add_many", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot, "_gate_lint", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_tests", fake_gate)
    monkeypatch.setattr(autopilot, "_commit_push_merge", fake_merge)
    monkeypatch.setattr(autopilot, "_wait_until_idle", fake_idle)


@pytest.mark.asyncio
async def test_shippable_not_completed_falls_through_to_merge(monkeypatch, tmp_path):
    """completed=False 但 shippable=True → 不早退 failed，續走閘門，最終 done（帶已知限制註記）。"""
    statuses: list = []
    gate_calls: list = []
    result = {
        "completed": False,
        "shippable": True,
        "followups": [],
        "followup_items": [],
        "core_changes": [],
    }
    _common_mocks(monkeypatch, tmp_path, result, statuses, gate_calls=gate_calls)

    await autopilot.run_one_task({"id": 9, "title": "尾票帶已知限制"})

    # 客觀閘門確實被執行（沒有早退）
    assert gate_calls, "shippable 應續走客觀閘門，不該在 completed 檢查早退"
    # 最終狀態 done，且帶已知限制註記
    assert statuses[-1][0:2] == (9, "done")
    assert "已知限制" in statuses[-1][2].get("note", "")
    # 全程沒有任何 failed
    assert not any(s[1] == "failed" for s in statuses)


def _not_shippable_result():
    return {
        "completed": False,
        "shippable": False,
        "followups": [],
        "followup_items": [],
        "core_changes": [],
    }


@pytest.mark.asyncio
async def test_not_shippable_first_attempt_retries_not_fails(monkeypatch, tmp_path):
    """completed=False 且 shippable=False，且還有重試額度 → 退回 pending 重試（非單發即 failed），
    且不進客觀閘門。討論未收斂常是暫時性的，重跑常會過（完成率最大失敗桶的修法）。"""
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 2)
    statuses: list = []
    gate_calls: list = []
    _common_mocks(monkeypatch, tmp_path, _not_shippable_result(), statuses, gate_calls=gate_calls)

    # 全新任務（attempts 未設＝0）：0+1=1 < 2 → 退回 pending 重試
    await autopilot.run_one_task({"id": 11, "title": "完全沒跑起來"})

    tid, status, kw = statuses[-1]
    assert (tid, status) == (11, "pending"), f"首次未收斂應退回 pending 重試：{statuses[-1]!r}"
    assert kw.get("attempts") == 1
    assert "討論未達完成" in kw.get("note", ""), "note 須保留『討論未達完成』子串以續接分診/看板"
    assert not any(s[1] == "failed" for s in statuses), "還有額度時不得標 failed"
    assert not gate_calls, "不可出貨應早退，不進客觀閘門"


@pytest.mark.asyncio
async def test_not_shippable_at_cap_fails(monkeypatch, tmp_path):
    """重試額度用罄（attempts 已達上限-1）→ 標 failed，note 仍含『討論未達完成』（分診/看板續接）。"""
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 2)
    statuses: list = []
    gate_calls: list = []
    _common_mocks(monkeypatch, tmp_path, _not_shippable_result(), statuses, gate_calls=gate_calls)

    # attempts=1：1+1=2 < 2 為 False → 永久 failed
    await autopilot.run_one_task({"id": 12, "title": "反覆跑不起來", "attempts": 1})

    tid, status, kw = statuses[-1]
    assert (tid, status) == (12, "failed"), f"用罄額度應標 failed：{statuses[-1]!r}"
    assert "討論未達完成" in kw.get("note", ""), "永久失敗 note 須含『討論未達完成』子串"
    assert not gate_calls, "不可出貨應早退，不進客觀閘門"
