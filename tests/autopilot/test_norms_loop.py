"""規範迴路(第 3 階 A3):人工介入+失敗事件 → FAST 蒸餾 → lessons(source=intervention)。

守護不變量:
- TI_NORMS_LOOP=0(預設)完全 no-op:零讀取、零 LLM。
- 每 UTC 日至多一次;無材料(無 output_review 介入且無失敗事件)直接跳過不呼叫 LLM。
- 只認「規範:」開頭行、至多 3 條、空行/雜訊丟棄;lessons.add_many(source=intervention)
  合法(_VALID_SOURCES 已含)。
- complete_once 拋錯/回空 → 不入庫、不影響主迴圈。
"""

from __future__ import annotations

import pytest

from studio import autonomy, autopilot, config, interventions, lessons, notify


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NORMS_LOOP", True)
    monkeypatch.setattr(autopilot, "_norms_distill_day", None)
    import studio.lessons as lessons_mod

    monkeypatch.setattr(lessons_mod, "_path", lambda: tmp_path / "lessons.json")
    monkeypatch.setattr(lessons_mod, "_read_cache", {}, raising=False)
    return tmp_path


def _fake_llm(monkeypatch, text):
    calls = {"n": 0}

    async def fake(system, user, *, session_id, cwd, timeout=120.0):
        calls["n"] += 1
        calls["user"] = user
        return text

    import studio.providers as providers_mod

    monkeypatch.setattr(providers_mod, "complete_once", fake)
    return calls


@pytest.mark.asyncio
async def test_disabled_by_default_noop(monkeypatch):
    monkeypatch.setattr(config, "NORMS_LOOP", False)
    calls = _fake_llm(monkeypatch, "規範: 不該出現")
    interventions.record("task_action", "output_review", task_id=1, detail="retry|要先跑測試")
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 0 and lessons.all_lessons() == []


@pytest.mark.asyncio
async def test_distills_notes_and_events_into_lessons(monkeypatch):
    interventions.record(
        "task_action", "output_review", task_id=1, detail="park|規格沒寫清楚就開工"
    )
    notify.record("gate_failure", gate="test", task_id=2)
    calls = _fake_llm(
        monkeypatch,
        "規範: 開工前先確認規格已寫清楚\n雜訊行\n規範: 測試閘門失敗先讀失敗輸出再改碼\n規範: 第三條\n規範: 第四條(超過上限)",
    )
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 1
    assert "規格沒寫清楚" in calls["user"] and "gate_failure" in calls["user"]
    texts = [it["text"] for it in lessons.all_lessons()]
    assert len(texts) == 3, "至多 3 條"
    assert texts[0] == "開工前先確認規格已寫清楚"
    assert all(it["source"] == "intervention" for it in lessons.all_lessons())
    event = next(
        row for row in autonomy.read_events() if row.get("outcome") == "norms_distill_screened"
    )
    assert event["payload"]["accepted_lessons"] == texts


@pytest.mark.asyncio
async def test_once_per_day(monkeypatch):
    interventions.record("task_action", "output_review", task_id=1, detail="x")
    calls = _fake_llm(monkeypatch, "無")
    await autopilot._maybe_norms_distill()
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 1, "同日只蒸餾一次"
    # deploy 後 execv 會清掉 module global，持久 marker 仍須維持同日一次。
    autopilot._norms_distill_day = None
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 1
    assert autopilot._norms_day_marker().is_file()

    autopilot._write_day_marker(autopilot._norms_day_marker(), "1970-01-01", "complete")
    autopilot._norms_distill_day = None
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 2, "舊日 marker 不得抑制今日蒸餾"


@pytest.mark.asyncio
async def test_no_material_skips_llm(monkeypatch):
    interventions.record("manual_task", "context_feeding", task_id=1, detail="補背景不算材料")
    calls = _fake_llm(monkeypatch, "規範: 不該出現")
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 0 and lessons.all_lessons() == []


@pytest.mark.asyncio
async def test_invalid_or_unwritable_claim_fails_closed_before_provider(monkeypatch):
    interventions.record("task_action", "output_review", task_id=1, detail="x")
    calls = _fake_llm(monkeypatch, "規範: 不得執行")
    autopilot._norms_day_marker().write_text("1970-01-01\n", encoding="utf-8")
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 0

    autopilot._norms_day_marker().unlink()
    autopilot._norms_distill_day = None

    def claim_disk_down(path, day, status="complete"):
        raise OSError("claim disk unavailable")

    monkeypatch.setattr(autopilot, "_write_day_marker", claim_disk_down)
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_llm_failure_swallowed(monkeypatch):
    interventions.record("task_action", "output_review", task_id=1, detail="x")

    async def boom(system, user, *, session_id, cwd, timeout=120.0):
        raise OSError("provider down")

    import studio.providers as providers_mod

    monkeypatch.setattr(providers_mod, "complete_once", boom)
    await autopilot._maybe_norms_distill()  # 不得拋
    assert lessons.all_lessons() == []

    # 失敗不寫成功 marker；模擬重啟後仍可重試，不會整日永久抑制。
    autopilot._norms_distill_day = None
    calls = _fake_llm(monkeypatch, "規範: provider 恢復後重新執行")
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 1
    assert [item["text"] for item in lessons.all_lessons()] == ["provider 恢復後重新執行"]


@pytest.mark.asyncio
async def test_completion_marker_failure_keeps_unknown_claim_without_llm_retry(monkeypatch):
    interventions.record("task_action", "output_review", task_id=1, detail="x")
    calls = _fake_llm(monkeypatch, "規範: 開工前先確認可驗收範圍")
    original_write = autopilot._write_day_marker

    writes = {"n": 0}

    def completion_marker_disk_down(path, day, status="complete"):
        writes["n"] += 1
        if writes["n"] >= 2:
            raise OSError("marker disk unavailable")
        return original_write(path, day, status)

    monkeypatch.setattr(autopilot, "_write_day_marker", completion_marker_disk_down)
    await autopilot._maybe_norms_distill()
    assert [item["text"] for item in lessons.all_lessons()] == ["開工前先確認可驗收範圍"]
    assert (
        autopilot._read_day_marker(autopilot._norms_day_marker(), autopilot._utc_day_string())
        == "in_progress"
    )

    monkeypatch.setattr(autopilot, "_write_day_marker", original_write)
    autopilot._norms_distill_day = None
    await autopilot._maybe_norms_distill()
    assert calls["n"] == 1
    assert [item["text"] for item in lessons.all_lessons()] == ["開工前先確認可驗收範圍"]
    assert (
        autopilot._read_day_marker(autopilot._norms_day_marker(), autopilot._utc_day_string())
        == "in_progress"
    )
