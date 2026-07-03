"""加速相關設定的單元測試:LEAD_ROLES 控制選模型、MAX_TASKS 控制拆解上限。"""

from __future__ import annotations

from studio import config, experts
from studio.orchestrator import parse_tasks
from studio.roles import BY_KEY


def test_model_for_respects_lead_roles(monkeypatch):
    # 解除 PM 模型釘選(預設釘 claude-fable-5,另測 tests/core/test_pm_pin.py),驗證 LEAD 二分法。
    monkeypatch.setattr(config, "PM_PIN_MODEL", "")
    monkeypatch.setattr(config, "LEAD_ROLES", {"pm"})
    assert experts._model_for(BY_KEY["pm"]) == config.MODEL_LEAD
    # 非 lead 角色一律走快速模型(加速)
    assert experts._model_for(BY_KEY["engineer"]) == config.MODEL_FAST
    assert experts._model_for(BY_KEY["senior"]) == config.MODEL_FAST


def test_lead_roles_can_be_widened(monkeypatch):
    monkeypatch.setattr(config, "LEAD_ROLES", {"pm", "senior"})
    assert experts._model_for(BY_KEY["senior"]) == config.MODEL_LEAD


def test_parse_tasks_caps_at_max_tasks(monkeypatch):
    monkeypatch.setattr(config, "MAX_TASKS", 3)
    text = "\n".join(f"任務: 做 t{i}" for i in range(10))
    assert len(parse_tasks(text)) == 3


def test_parse_tasks_bullet_fallback_capped(monkeypatch):
    monkeypatch.setattr(config, "MAX_TASKS", 2)
    text = "- 第一件\n- 第二件\n- 第三件\n- 第四件"
    assert len(parse_tasks(text)) == 2
