"""調查結論對抗性驗證（refuter）：done 前一次廉價呼叫試圖推翻結論。

對治調查分流的已知風險「單專家自說自話」（reward hacking）：結論寫得頭頭是道、證據
卻對不上，且結論會進教訓庫污染長期記憶。契約：
- 反駁成立 → 不標 done、不進教訓庫；走「討論未達完成」重試語意，note 帶破綻。
- 反駁不成立 → 照常 done。
- refuter 空輸出/例外（離線/逾時）→ 照常 done——**寧放勿殺**，refuter 是加值防線不是依賴。
- 旋鈕 TI_AUTOPILOT_INVESTIGATION_REFUTE=0 → 完全不呼叫。

harness 沿用 test_investigation_lane.py；complete_once 以 monkeypatch 控制，零 LLM/網路。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config, flow, providers

_CONCLUSION_REPLY = "結論: 根因是 watchdog 未涵蓋 fetch 階段\n證據: studio/runner.py:120\n"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", True)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_REFUTE", True)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_TIMEOUT", 30)
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 2)
    return tmp_path


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    class _FakeExpert:
        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            return reply

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)


def _patch_refuter(monkeypatch, reply):
    calls: list[dict] = []

    async def _fake(system, user, *, session_id, cwd, timeout=120.0):
        calls.append({"system": system, "user": user, "session_id": session_id})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(providers, "complete_once", _fake)
    return calls


def _patch_lessons(monkeypatch):
    import studio.lessons as lessons_mod

    recorded: list = []
    monkeypatch.setattr(lessons_mod, "add_many", lambda texts, **kw: recorded.append(texts) or 1)
    return recorded


def _load(task_id):
    return next(t for t in backlog.list_tasks() if t["id"] == task_id)


@pytest.mark.asyncio
async def test_refuted_conclusion_retries_instead_of_done(state, monkeypatch):
    _patch_expert(monkeypatch, _CONCLUSION_REPLY)
    calls = _patch_refuter(
        monkeypatch, "分析過程…\n反駁: 成立 證據行號指向 runner，與 fetch 結論無關"
    )
    lessons = _patch_lessons(monkeypatch)
    task = backlog.add("調查 X 的根因並回報")
    backlog.set_status(task["id"], "in_progress")

    await autopilot._run_investigation_task(task, "/clone", "sid-r1", 0.0)

    updated = _load(task["id"])
    assert updated["status"] == "pending", "被反駁不得標 done，走重試語意"
    assert "討論未達完成" in updated["note"] and "被反駁" in updated["note"]
    assert "行號指向 runner" in updated["note"], "note 須帶破綻供重查"
    assert not lessons, "被反駁的結論不得進教訓庫"
    assert calls and "watchdog" in calls[0]["user"], "refuter 須收到結論全文"


@pytest.mark.asyncio
async def test_not_refuted_marks_done(state, monkeypatch):
    _patch_expert(monkeypatch, _CONCLUSION_REPLY)
    _patch_refuter(monkeypatch, "看過了，證據能支撐。\n反駁: 不成立")
    lessons = _patch_lessons(monkeypatch)
    task = backlog.add("調查 X 的根因並回報")

    await autopilot._run_investigation_task(task, "/clone", "sid-r2", 0.0)

    assert _load(task["id"])["status"] == "done"
    assert lessons, "通過 refuter 的結論照常進教訓庫"


@pytest.mark.asyncio
@pytest.mark.parametrize("refuter_reply", ["", "我壞掉了忘記照格式輸出", RuntimeError("offline")])
async def test_refuter_failure_never_blocks_done(state, monkeypatch, refuter_reply):
    """寧放勿殺：refuter 空輸出/壞格式/例外都不得擋住合法結論。"""
    _patch_expert(monkeypatch, _CONCLUSION_REPLY)
    _patch_refuter(monkeypatch, refuter_reply)
    _patch_lessons(monkeypatch)
    task = backlog.add("調查 X 的根因並回報")

    await autopilot._run_investigation_task(task, "/clone", "sid-r3", 0.0)

    assert _load(task["id"])["status"] == "done"


@pytest.mark.asyncio
async def test_knob_off_skips_refuter_entirely(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_REFUTE", False)
    _patch_expert(monkeypatch, _CONCLUSION_REPLY)
    calls = _patch_refuter(monkeypatch, "反駁: 成立 不該被看到")
    _patch_lessons(monkeypatch)
    task = backlog.add("調查 X 的根因並回報")

    await autopilot._run_investigation_task(task, "/clone", "sid-r4", 0.0)

    assert _load(task["id"])["status"] == "done"
    assert not calls, "旋鈕關閉不得呼叫 refuter"


@pytest.mark.asyncio
async def test_refuted_audit_outcome_recorded(state, monkeypatch, tmp_path):
    import json

    _patch_expert(monkeypatch, _CONCLUSION_REPLY)
    _patch_refuter(monkeypatch, "反駁: 成立 答非所問")
    _patch_lessons(monkeypatch)
    task = backlog.add("調查 X 的根因並回報")
    backlog.set_status(task["id"], "in_progress")

    await autopilot._run_investigation_task(task, "/clone", "sid-r5", 0.0)

    lines = [
        json.loads(line)
        for line in (tmp_path / "ap" / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert lines[-1]["outcome"] == "investigation_refuted"
    assert "答非所問" in lines[-1]["detail"]


# --- flow.parse_refutation 純函式 -------------------------------------------


def test_parse_refutation_markers():
    assert flow.parse_refutation("反駁: 成立 證據對不上") == "證據對不上"
    assert flow.parse_refutation("反駁: 不成立") == ""
    assert flow.parse_refutation("反駁：成立") != "", "成立但沒附破綻也要非空（帶預設說明）"
    assert flow.parse_refutation("") == ""
    assert flow.parse_refutation("完全沒有標記的輸出") == ""
    # 多個取最後（與 _last_match 慣例一致）
    assert flow.parse_refutation("反駁: 成立 舊\n重想後——\n反駁: 不成立") == ""
