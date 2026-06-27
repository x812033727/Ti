"""task_pipeline 資料驅動：_work_task 讀 workflow 的 build.task_pipeline 決定 security 審查
與 critic 閘門是否啟用。預設 workflow 重現今日（security+critic 皆在），客製可省略。
"""

from __future__ import annotations

from studio.orchestrator import StudioSession


async def _noop(ev):
    pass


def _session(workflow=None):
    return StudioSession("t", _noop, workflow=workflow)


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
