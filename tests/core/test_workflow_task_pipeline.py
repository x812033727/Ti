"""task_pipeline 資料驅動：_work_task 讀 workflow 的 build.task_pipeline 決定實作者、reviewer
集合（含非核心角色）、critic 閘門與輪數。預設 workflow 重現今日，客製可增刪/換人。
"""

from __future__ import annotations

import pytest

from studio import events
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


async def _noop(ev):
    pass


def _session(workflow=None):
    return StudioSession("t", _noop, workflow=workflow)


class StubExpert:
    """依角色回傳腳本化回應，記錄被呼叫次數與收到的 prompt。"""

    def __init__(self, role: Role, script: str):
        self.role = role
        self._script = script
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        await broadcast(
            events.expert_message(
                "t", self.role.key, self.role.name, self.role.avatar, self._script
            )
        )
        return self._script

    async def stop(self) -> None:
        pass


def test_default_keeps_security_and_critic():
    s = _session()  # workflow=None → 內建預設
    assert s._task_review_role_keys() == {"qa", "senior", "security"}
    assert s._task_critic_enabled() is True


def test_custom_review_gate_drops_security():
    wf = {
        "name": "無資安",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {"type": "review", "gate": [{"role": "qa", "verdict": "qa_passed"}]},
                    {"type": "gate", "gate": [{"role": "pm", "verdict": "critic_blocks"}]},
                ],
            }
        ],
    }
    s = _session(wf)
    assert "security" not in s._task_review_role_keys()
    assert s._task_critic_enabled() is True  # 仍有 gate stage


def test_custom_without_gate_stage_drops_critic():
    wf = {
        "name": "無 critic",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {
                        "type": "review",
                        "gate": [
                            {"role": "qa", "verdict": "qa_passed"},
                            {"role": "senior", "verdict": "senior_approved"},
                        ],
                    },
                ],
            }
        ],
    }
    s = _session(wf)
    assert s._task_critic_enabled() is False
    assert s._task_review_role_keys() == {"qa", "senior"}


def test_no_build_stage_falls_back_to_defaults():
    wf = {"name": "無 build", "stages": [{"type": "decompose"}, {"type": "demo"}]}
    s = _session(wf)
    assert s._task_review_role_keys() == {"qa", "senior", "security"}
    assert s._task_critic_enabled() is True


# --- 資料驅動 reviewer / implementer / max_rounds 的存取器 -------------------


def test_reviewers_default_ordered_and_filtered():
    experts = {k: object() for k in ("pm", "engineer", "qa", "senior", "security")}
    s = _session()
    assert s._task_reviewers(experts) == [
        ("qa", "qa_passed"),
        ("senior", "senior_approved"),
        ("security", "security_approved"),
    ]
    # security 不在場 → 自動濾掉（重現今日 has_security）。
    no_sec = {k: object() for k in ("pm", "engineer", "qa", "senior")}
    assert s._task_reviewers(no_sec) == [("qa", "qa_passed"), ("senior", "senior_approved")]


def test_reviewers_custom_gate_with_noncore_role():
    wf = {
        "name": "客製審",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {
                        "type": "review",
                        "gate": [
                            {"role": "qa", "verdict": "qa_passed"},
                            {"role": "architect", "verdict": "senior_approved"},
                        ],
                    },
                ],
            }
        ],
    }
    s = _session(wf)
    experts = {k: object() for k in ("pm", "engineer", "qa", "senior", "architect")}
    assert s._task_reviewers(experts) == [
        ("qa", "qa_passed"),
        ("architect", "senior_approved"),
    ]


def test_implementer_and_max_rounds():
    s = _session()
    assert s._task_implementer() == "engineer"
    assert s._task_max_rounds() is None
    wf = {
        "name": "客製",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "architect"},
                    {
                        "type": "review",
                        "max_rounds": 5,
                        "gate": [{"role": "qa", "verdict": "qa_passed"}],
                    },
                ],
            }
        ],
    }
    s2 = _session(wf)
    assert s2._task_implementer() == "architect"
    assert s2._task_max_rounds() == 5


# --- 端到端：_work_task 走客製 reviewer / implementer（cwd=None，不需 git/沙箱）-------


def _experts_with(*keys, script="決議: 核可"):
    scripts = {
        "engineer": "已實作完成",
        "qa": "驗證: PASS",
        "senior": "決議: 核可",
        "security": "決議: 安全核可",
        "architect": "決議: 核可",
        "pm": "決議: 完成",
    }
    return {k: StubExpert(BY_KEY[k], scripts.get(k, script)) for k in keys}


@pytest.mark.asyncio
async def test_work_task_runs_custom_reviewer_and_drops_core_senior():
    # review gate = [qa, architect]，無 gate stage（跳過 critic）。senior 不在 gate → 不審。
    wf = {
        "name": "客製審",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {
                        "type": "review",
                        "gate": [
                            {"role": "qa", "verdict": "qa_passed"},
                            {"role": "architect", "verdict": "senior_approved"},
                        ],
                    },
                ],
            }
        ],
    }
    experts = _experts_with("pm", "engineer", "qa", "senior", "architect")
    s = StudioSession("t", _noop, experts=experts, cwd=None, workflow=wf)
    ctx = LaneContext("main", None, experts, None)
    ok = await s._work_task(ctx, {"id": 1, "title": "做個東西", "status": "todo"}, "計畫")
    assert ok is True
    assert experts["engineer"].calls >= 1  # 實作者
    assert experts["qa"].calls >= 1  # reviewer
    assert experts["architect"].calls >= 1  # 客製 reviewer 有審
    assert experts["senior"].calls == 0  # 不在 review gate → 不審


@pytest.mark.asyncio
async def test_work_task_uses_custom_implementer():
    # implement.assignee = architect → architect 實作；review gate = [qa]。
    wf = {
        "name": "客製實作者",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "architect"},
                    {"type": "review", "gate": [{"role": "qa", "verdict": "qa_passed"}]},
                ],
            }
        ],
    }
    experts = _experts_with("pm", "engineer", "qa", "architect")
    experts["architect"] = StubExpert(BY_KEY["architect"], "已實作完成")
    s = StudioSession("t", _noop, experts=experts, cwd=None, workflow=wf)
    ctx = LaneContext("main", None, experts, None)
    ok = await s._work_task(ctx, {"id": 1, "title": "做個東西", "status": "todo"}, "計畫")
    assert ok is True
    assert experts["architect"].calls >= 1  # 客製實作者有發言（實作）
    assert experts["engineer"].calls == 0  # 預設 engineer 未被用作實作者


# --- task 級 dynamic stage：PM 任務內動態追加把關 --------------------------


def test_task_dynamic_stage_detection():
    assert _session()._task_dynamic_stage() is None  # 預設無
    wf = {
        "name": "追加把關",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {"type": "review", "gate": [{"role": "qa", "verdict": "qa_passed"}]},
                    {"type": "dynamic", "budget": 1, "fallback": "engineer"},
                ],
            }
        ],
    }
    st = _session(wf)._task_dynamic_stage()
    assert st is not None and st["type"] == "dynamic" and st["budget"] == 1


def _dynamic_consult_wf():
    return {
        "name": "追加把關",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {
                        "type": "review",
                        "max_rounds": 2,
                        "gate": [{"role": "qa", "verdict": "qa_passed"}],
                    },
                    {"type": "dynamic", "budget": 1, "fallback": "engineer"},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_task_dynamic_consult_passes_when_no_objection():
    experts = _experts_with("pm", "engineer", "qa")
    experts["pm"] = StubExpert(BY_KEY["pm"], "下一步: architect\n指示: 複核安全性")
    experts["architect"] = StubExpert(BY_KEY["architect"], "異議: 不成立")
    s = StudioSession("t", _noop, experts=experts, cwd=None, workflow=_dynamic_consult_wf())
    ctx = LaneContext("main", None, experts, None)
    ok = await s._work_task(ctx, {"id": 1, "title": "做個東西", "status": "todo"}, "計畫")
    assert ok is True
    assert experts["pm"].calls >= 1  # PM 有決定追加把關
    assert experts["architect"].calls >= 1  # 被追加的專家有發言
    assert experts["architect"].calls == experts["pm"].calls  # 每次 PM 派人就諮詢一次


@pytest.mark.asyncio
async def test_task_dynamic_consult_block_forces_retry_and_fail():
    # 追加把關每輪都提異議成立 → 退回再修，撐到 review.max_rounds=2 仍未過 → 任務失敗。
    experts = _experts_with("pm", "engineer", "qa")
    experts["pm"] = StubExpert(BY_KEY["pm"], "下一步: architect\n指示: 複核")
    experts["architect"] = StubExpert(BY_KEY["architect"], "異議: 成立 有嚴重風險")
    s = StudioSession("t", _noop, experts=experts, cwd=None, workflow=_dynamic_consult_wf())
    ctx = LaneContext("main", None, experts, None)
    ok = await s._work_task(ctx, {"id": 1, "title": "做個東西", "status": "todo"}, "計畫")
    assert ok is False  # 追加把關阻擋、撐滿輪數仍未過
    assert experts["architect"].calls >= 2  # 每輪都被追加諮詢
    assert experts["engineer"].calls >= 2  # 每輪都重新實作


@pytest.mark.asyncio
async def test_task_dynamic_consult_end_skips():
    # PM 一開口就 `下一步: 結束` → 不諮詢任何人、直接放行。
    experts = _experts_with("pm", "engineer", "qa")
    experts["pm"] = StubExpert(BY_KEY["pm"], "下一步: 結束")
    experts["architect"] = StubExpert(BY_KEY["architect"], "異議: 成立")
    s = StudioSession("t", _noop, experts=experts, cwd=None, workflow=_dynamic_consult_wf())
    ctx = LaneContext("main", None, experts, None)
    ok = await s._work_task(ctx, {"id": 1, "title": "做個東西", "status": "todo"}, "計畫")
    assert ok is True
    assert experts["architect"].calls == 0  # PM 選擇結束 → 沒有追加任何人
