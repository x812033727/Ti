"""意圖迴路自動化(軌 G2):旗標雙閘/每日 throttle/任務與核心改動分流/失敗不冒泡。"""

from __future__ import annotations

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
    reply = "任務: [P1/improvement] 修意圖缺口X\n核心改動: 改核心Y\n"

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
    assert [(t["title"], t["source"]) for t in proj_tasks] == [("修意圖缺口X", "intent")]
    core = [t for t in backlog.list_tasks() if t["title"] == "改核心Y"]
    assert len(core) == 1 and core[0]["source"] == "intent"
    # 每日 throttle:同日第二次零成本
    await autopilot._maybe_intent_discovery()
    assert len(fake_expert.calls) == 1


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
