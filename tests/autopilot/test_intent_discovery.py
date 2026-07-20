"""意圖迴路自動化(軌 G2):旗標雙閘/每日 throttle/任務與核心改動分流/失敗不冒泡。"""

from __future__ import annotations

import json

import pytest

from studio import autonomy, autopilot, backlog, config, experts, projects


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws", raising=False)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    monkeypatch.setattr(autopilot, "_intent_discovery_day", None)
    monkeypatch.setattr(config, "INTENT_LOOP", True)
    monkeypatch.setattr(config, "INTENT_DISCOVERY", True)
    return tmp_path


class _FakeExpert:
    calls: list[str] = []
    reply = "任務: [P1/improvement] 修正意圖流程缺口X\n核心改動: 修改核心路由流程Y\n"

    def __init__(self, role, sid, cwd):
        self.sid = sid

    async def speak(self, prompt, broadcast):
        _FakeExpert.calls.append(prompt)
        return _FakeExpert.reply

    async def stop(self):
        return None


@pytest.fixture()
def fake_expert(monkeypatch):
    _FakeExpert.calls = []
    monkeypatch.setattr(experts, "Expert", _FakeExpert)
    return _FakeExpert


def _mkproject_with_intent(intent="把預約流程做到順"):
    meta = projects.create("測試產品", vision="v")
    assert meta is not None
    projects.set_intent(meta["id"], intent)
    projects.workspace_dir(meta["id"]).mkdir(parents=True, exist_ok=True)
    return meta


def test_daily_automation_lock_is_cross_handle_exclusive():
    first = autopilot._acquire_daily_automation_lock("intent_discovery_test")
    assert first is not None
    try:
        assert autopilot._acquire_daily_automation_lock("intent_discovery_test") is None
    finally:
        autopilot._release_daily_automation_lock(first)
    third = autopilot._acquire_daily_automation_lock("intent_discovery_test")
    assert third is not None
    autopilot._release_daily_automation_lock(third)


@pytest.mark.asyncio
async def test_flag_gating_noop(monkeypatch, fake_expert):
    _mkproject_with_intent()
    monkeypatch.setattr(config, "INTENT_DISCOVERY", False)
    await autopilot._maybe_intent_discovery()
    monkeypatch.setattr(config, "INTENT_DISCOVERY", True)
    monkeypatch.setattr(config, "INTENT_LOOP", False)
    monkeypatch.setattr(autopilot, "_intent_discovery_day", None)
    await autopilot._maybe_intent_discovery()
    assert fake_expert.calls == [], "任一旗標關=零成本"


@pytest.mark.asyncio
async def test_discovery_routes_tasks_and_core_changes(fake_expert):
    meta = _mkproject_with_intent()
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1 and "把預約流程做到順" in fake_expert.calls[0]
    proj_tasks = backlog.list_tasks(state_dir=projects.state_dir(meta["id"]))
    assert [(t["title"], t["source"]) for t in proj_tasks] == [("修正意圖流程缺口X", "intent")]
    core = [t for t in backlog.list_tasks() if t["title"] == "修改核心路由流程Y"]
    assert len(core) == 1 and core[0]["source"] == "intent"
    decision = next(
        event
        for event in autonomy.read_events()
        if event.get("project_id") == meta["id"]
        and event.get("outcome") == "intent_discovery_screened"
    )
    assert decision["payload"]["project_accepted_titles"] == ["修正意圖流程缺口X"]
    assert decision["payload"]["core_accepted_titles"] == ["修改核心路由流程Y"]
    assert decision["payload"]["project_proposed_titles"] == ["修正意圖流程缺口X"]
    assert decision["payload"]["core_proposed_titles"] == ["修改核心路由流程Y"]
    # 每日 throttle:同日第二次零成本
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1
    # 模擬 deploy 後 execv：行程記憶體歸零，持久 marker 仍須擋同日重跑。
    autopilot._intent_discovery_day = None
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1
    assert autopilot._intent_discovery_marker().is_file()

    # marker 是舊日即應再跑；語意去重同時保證重跑不會複製既有任務。
    marker = autopilot._intent_discovery_marker()
    state = json.loads(marker.read_text(encoding="utf-8"))
    state["projects"][meta["id"]] = {
        "day": "1970-01-01",
        "status": "complete",
        "updated_at": 0,
    }
    marker.write_text(json.dumps(state), encoding="utf-8")
    autopilot._intent_discovery_day = None
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 2
    assert len(backlog.list_tasks(state_dir=projects.state_dir(meta["id"]))) == 1


@pytest.mark.asyncio
async def test_no_intent_projects_skipped(fake_expert):
    projects.create("無意圖專案", vision="v")
    await autopilot._maybe_intent_discovery()
    assert fake_expert.calls == []


@pytest.mark.asyncio
async def test_expert_failure_swallowed(monkeypatch, fake_expert):
    _mkproject_with_intent()

    async def boom(self, prompt, broadcast):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(_FakeExpert, "speak", boom)
    await autopilot._maybe_intent_discovery()  # 不得冒泡


@pytest.mark.asyncio
async def test_success_is_persisted_per_project_and_failed_project_can_retry(
    monkeypatch, fake_expert
):
    first = _mkproject_with_intent("第一專案意圖")
    second = _mkproject_with_intent("第二專案意圖")
    calls: list[str] = []

    async def fail_first(self, prompt, broadcast):
        calls.append(self.sid)
        if first["id"][:8] in self.sid:
            raise RuntimeError("first project down")
        return _FakeExpert.reply

    monkeypatch.setattr(_FakeExpert, "speak", fail_first)
    await autopilot._maybe_intent_discovery()
    assert backlog.list_tasks(state_dir=projects.state_dir(first["id"])) == []
    assert len(backlog.list_tasks(state_dir=projects.state_dir(second["id"]))) == 1

    async def succeed(self, prompt, broadcast):
        calls.append(self.sid)
        return _FakeExpert.reply

    # 模擬重啟：成功專案由持久 marker 跳過，失敗專案可獨立重試。
    monkeypatch.setattr(_FakeExpert, "speak", succeed)
    autopilot._intent_discovery_day = None
    await autopilot._maybe_intent_discovery()
    assert len(backlog.list_tasks(state_dir=projects.state_dir(first["id"]))) == 1
    assert len(backlog.list_tasks(state_dir=projects.state_dir(second["id"]))) == 1
    assert sum(second["id"][:8] in sid for sid in calls) == 1
    assert sum(first["id"][:8] in sid for sid in calls) == 2


@pytest.mark.asyncio
async def test_strict_parser_and_quality_gate_never_enqueue_generic_fallback(
    monkeypatch, fake_expert
):
    meta = _mkproject_with_intent()
    monkeypatch.setattr(
        _FakeExpert,
        "reply",
        "任務: 實作需求\n核心改動: 修改核心發布驗證流程\n",
    )
    await autopilot._maybe_intent_discovery()
    assert backlog.list_tasks(state_dir=projects.state_dir(meta["id"])) == []
    assert all(task["title"] != "實作需求" for task in backlog.list_tasks())
    assert any(task["title"] == "修改核心發布驗證流程" for task in backlog.list_tasks())


@pytest.mark.asyncio
async def test_intent_discovery_dedups_active_and_same_batch_semantic_rewrites(
    monkeypatch, fake_expert
):
    meta = _mkproject_with_intent()
    sdir = projects.state_dir(meta["id"])
    existing = (
        "在 `_confirm_text` 的建單成功回覆補上「預約日期時間＋服務名稱」："
        "replies.py:498 目前只顯示預約編號"
    )
    backlog.add(existing, source="intent", state_dir=sdir)
    monkeypatch.setattr(
        _FakeExpert,
        "reply",
        "任務: 在 _confirm_text（replies.py:494-549）建單成功訊息內補上預約日期時間\n"
        "任務: 在 `_confirm_text` 建單成功回覆補上預約時間與服務名稱\n"
        "任務: 在 `_available_slots` 過濾已經過去的預約時段\n",
    )
    await autopilot._maybe_intent_discovery()
    titles = [task["title"] for task in backlog.list_tasks(state_dir=sdir)]
    assert titles == [existing, "在 `_available_slots` 過濾已經過去的預約時段"]


@pytest.mark.asyncio
async def test_corrupt_marker_and_audit_failure_fail_closed(monkeypatch, fake_expert):
    meta = _mkproject_with_intent()
    marker = autopilot._intent_discovery_marker()
    marker.write_text("{broken", encoding="utf-8")
    await autopilot._maybe_intent_discovery()
    assert fake_expert.calls == []
    assert backlog.list_tasks(state_dir=projects.state_dir(meta["id"])) == []

    marker.unlink()
    autopilot._intent_discovery_day = None

    def audit_down(*args, **kwargs):
        raise autonomy.AuditWriteError("disk unavailable")

    monkeypatch.setattr(autonomy, "emit_event", audit_down)
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1
    assert backlog.list_tasks(state_dir=projects.state_dir(meta["id"])) == []
    state = json.loads(marker.read_text(encoding="utf-8"))
    assert state["projects"][meta["id"]]["status"] == "in_progress"
    autopilot._intent_discovery_day = None
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1, "LLM 已回覆後 audit 失敗屬未知結果，不得自動重呼叫"


@pytest.mark.asyncio
async def test_completion_marker_failure_keeps_unknown_claim_without_llm_retry(
    monkeypatch, fake_expert
):
    meta = _mkproject_with_intent()
    original_write = autopilot._write_intent_discovery_days

    writes = {"n": 0}

    def completion_marker_disk_down(days):
        writes["n"] += 1
        if writes["n"] >= 2:
            raise OSError("marker disk unavailable")
        return original_write(days)

    monkeypatch.setattr(autopilot, "_write_intent_discovery_days", completion_marker_disk_down)
    await autopilot._maybe_intent_discovery()
    sdir = projects.state_dir(meta["id"])
    assert len(backlog.list_tasks(state_dir=sdir)) == 1
    assert len(backlog.list_tasks()) == 1
    state = json.loads(autopilot._intent_discovery_marker().read_text(encoding="utf-8"))
    assert state["projects"][meta["id"]]["status"] == "in_progress"

    # crash/restart window：backlog 已寫但 complete marker 未寫。durable claim 的
    # in_progress 是未知結果，下一行程必須 fail closed，不能用不同 LLM 回覆重做。
    monkeypatch.setattr(autopilot, "_write_intent_discovery_days", original_write)
    autopilot._intent_discovery_day = None
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1
    assert len(backlog.list_tasks(state_dir=sdir)) == 1
    assert len(backlog.list_tasks()) == 1
    state = json.loads(autopilot._intent_discovery_marker().read_text(encoding="utf-8"))
    assert state["projects"][meta["id"]]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_stage4_policy_drives_discovery_when_legacy_flags_are_off(monkeypatch, fake_expert):
    meta = _mkproject_with_intent("")
    autonomy.save_policy(
        meta["id"],
        {
            "stage": 4,
            "intent": {
                "north_star": "把付款成功率提高到 99%",
                "success_metrics": ["payment_success>=0.99"],
                "forbidden_actions": ["不得清除訂單"],
            },
        },
    )
    monkeypatch.setattr(config, "INTENT_DISCOVERY", False)
    monkeypatch.setattr(config, "INTENT_LOOP", False)
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1
    assert "Stage 4 版本化規畫證據" in fake_expert.calls[0]
    assert "payment_success>=0.99" in fake_expert.calls[0]
