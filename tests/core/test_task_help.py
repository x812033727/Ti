"""中途求助 PM：工程師實作發言含 `求助: <問題>` 時，就地讓 PM 給指示、工程師續作。

涵蓋 parser 純函式、觸發/開關/PM 缺席/上限（每任務語意）與 hint 注入條件。
"""

from __future__ import annotations

from studio import config, events
from studio.flow import parse_help_request
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role
from studio.workflow import fast_track_workflow


async def _noop(ev):
    pass


class ScriptedExpert:
    """多段腳本 stub：第 N 次呼叫回第 N 段，超出取最後一段；記錄 prompts 供斷言。"""

    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


def _experts(engineer_scripts: list[str], *, with_pm: bool = True, qa_scripts=None):
    experts = {
        "engineer": ScriptedExpert(BY_KEY["engineer"], engineer_scripts),
        "qa": ScriptedExpert(BY_KEY["qa"], qa_scripts or ["驗證: PASS"]),
    }
    if with_pm:
        experts["pm"] = ScriptedExpert(BY_KEY["pm"], ["先用 sqlite 存檔即可，別碰外部服務"])
    return experts


async def _run_task(experts) -> bool:
    # 快速模式 workflow：qa 單審、無任務級 gate/dynamic → pm.calls 純算求助路徑。
    s = StudioSession("t", _noop, experts=experts, cwd=None, workflow=fast_track_workflow())
    ctx = LaneContext("main", None, experts, None)
    return await s._work_task(ctx, {"id": 1, "title": "做個東西", "status": "todo"}, "計畫")


# --- parser 純函式 -----------------------------------------------------------


def test_parse_help_request_basic_and_fullwidth():
    assert parse_help_request("寫到一半\n求助: 該用哪個儲存方案？") == "該用哪個儲存方案？"
    assert parse_help_request("求助： 全形冒號也要通") == "全形冒號也要通"


def test_parse_help_request_last_match_and_empty():
    text = "求助: 第一個問題\n中間繼續寫\n求助: 第二個問題"
    assert parse_help_request(text) == "第二個問題"
    assert parse_help_request("沒有標記的普通發言") == ""
    assert parse_help_request("") == ""


# --- 觸發／開關／缺席／上限 ---------------------------------------------------


async def test_help_triggers_pm_then_engineer_resumes(monkeypatch):
    monkeypatch.setattr(config, "TASK_HELP_ENABLED", True)
    monkeypatch.setattr(config, "TASK_HELP_MAX", 1)
    experts = _experts(["寫到一半\n求助: 這裡該用哪個儲存方案？", "已依 PM 指示完成"])
    ok = await _run_task(experts)
    assert ok is True
    assert experts["pm"].calls == 1  # PM 被就地諮詢一次
    assert experts["engineer"].calls == 2  # 求助後續作一次
    assert "這裡該用哪個儲存方案？" in experts["pm"].prompts[0]  # 問題原文帶給 PM
    assert "先用 sqlite" in experts["engineer"].prompts[1]  # PM 指示帶回工程師


async def test_help_disabled_skips_pm(monkeypatch):
    monkeypatch.setattr(config, "TASK_HELP_ENABLED", False)
    experts = _experts(["寫到一半\n求助: 有問題", "已完成"])
    ok = await _run_task(experts)
    assert ok is True
    assert experts["pm"].calls == 0  # 開關關 → 不觸發
    assert experts["engineer"].calls == 1
    assert "求助" not in experts["engineer"].prompts[0]  # hint 注入受同開關控制


async def test_help_without_pm_is_safe(monkeypatch):
    monkeypatch.setattr(config, "TASK_HELP_ENABLED", True)
    monkeypatch.setattr(config, "TASK_HELP_MAX", 1)
    experts = _experts(["寫到一半\n求助: 有問題", "已完成"], with_pm=False)
    ok = await _run_task(experts)  # PM 缺席 → 安全跳過、不拋例外
    assert ok is True
    assert experts["engineer"].calls == 1
    assert "求助" not in experts["engineer"].prompts[0]  # PM 缺席也不承諾 marker


async def test_help_max_caps_repeated_requests(monkeypatch):
    monkeypatch.setattr(config, "TASK_HELP_ENABLED", True)
    monkeypatch.setattr(config, "TASK_HELP_MAX", 1)
    # 續作仍求助 → 第二次被上限擋下，流程照常往下走。
    experts = _experts(["求助: 問題A", "求助: 問題B（不該再被受理）"])
    ok = await _run_task(experts)
    assert ok is True
    assert experts["pm"].calls == 1

    monkeypatch.setattr(config, "TASK_HELP_MAX", 2)
    experts2 = _experts(["求助: 問題A", "求助: 問題B", "已完成"])
    ok2 = await _run_task(experts2)
    assert ok2 is True
    assert experts2["pm"].calls == 2
    assert experts2["engineer"].calls == 3


async def test_help_max_is_per_task_not_per_round(monkeypatch):
    monkeypatch.setattr(config, "TASK_HELP_ENABLED", True)
    monkeypatch.setattr(config, "TASK_HELP_MAX", 1)
    # QA 第一輪退回 → 第二輪工程師再求助，但額度已用盡 → 全程只諮詢 PM 一次。
    experts = _experts(
        ["求助: 第一輪的問題", "第一輪續作完成", "求助: 第二輪又卡了", "第二輪完成"],
        qa_scripts=["驗證: FAIL 缺測試", "驗證: PASS"],
    )
    ok = await _run_task(experts)
    assert ok is True
    assert experts["pm"].calls == 1  # 每任務上限，不隨輪數重置


async def test_help_hint_injected_when_available(monkeypatch):
    monkeypatch.setattr(config, "TASK_HELP_ENABLED", True)
    monkeypatch.setattr(config, "TASK_HELP_MAX", 1)
    experts = _experts(["直接完成，不求助"])
    ok = await _run_task(experts)
    assert ok is True
    assert "求助: " in experts["engineer"].prompts[0]  # 開關開＋PM 在場 → 有提示
    assert experts["pm"].calls == 0  # 沒求助就零額外呼叫
