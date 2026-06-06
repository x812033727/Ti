"""用 stub 專家測試工作流程狀態機，不呼叫真正的 LLM。"""

from __future__ import annotations

import pytest

from studio import events
from studio.orchestrator import (
    StudioSession,
    parse_tasks,
    pm_done,
    qa_passed,
    senior_approved,
)
from studio.roles import BY_KEY, Role


class StubExpert:
    """依角色給腳本化回應，並記錄被呼叫次數。"""

    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message(
                "t", self.role.key, self.role.name, self.role.avatar, text
            )
        )
        return text

    async def stop(self) -> None:
        pass


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


def types(bucket):
    return [e.type for e in bucket]


# --- 決議解析 -----------------------------------------------------------

def test_qa_passed_parsing():
    assert qa_passed("跑了測試\n驗證: PASS")
    assert not qa_passed("有錯\n驗證：FAIL")
    assert qa_passed("一切正常")          # 後備：無明顯失敗字
    assert not qa_passed("test failed")   # 後備：偵測到失敗


def test_senior_parsing():
    assert senior_approved("看起來不錯\n決議: 核可")
    assert not senior_approved("有問題\n決議：退回")


def test_pm_done_parsing():
    assert pm_done("符合\n決議: 完成")
    assert not pm_done("還缺測試\n決議：未完成")


def test_parse_tasks():
    text = "任務清單:\n- 建立 CLI\n- 加入分類邏輯\n2. 寫說明"
    tasks = parse_tasks(text)
    assert "建立 CLI" in tasks
    assert "寫說明" in tasks
    assert parse_tasks("沒有條列") == ["實作需求"]


# --- 流程 ---------------------------------------------------------------

def _experts(pm, eng, qa, senior):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }


@pytest.mark.asyncio
async def test_happy_path_one_round():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務清單:\n- 實作 BMI", "決議: 完成", "做得不錯，下次可加更多測試"],
        eng=["已建立 bmi.py"],
        qa=["測試全過\n驗證: PASS"],
        senior=["品質良好\n決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("做一個 BMI CLI")

    ts = types(bucket)
    assert events.EventType.SESSION_STARTED in ts
    assert events.EventType.DONE in ts
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True
    # 一輪就過：工程師只被呼叫一次
    assert experts["engineer"].calls == 1


@pytest.mark.asyncio
async def test_retry_then_pass():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務清單:\n- 實作", "決議: 完成", "檢討完畢"],
        eng=["第一版", "已修正"],
        qa=["有錯\n驗證: FAIL", "修好了\n驗證: PASS"],
        senior=["先不核可\n決議: 退回", "可以了\n決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 第一輪失敗 → 工程師應被呼叫兩次
    assert experts["engineer"].calls == 2
    results = [e for e in bucket if e.type == events.EventType.RUN_RESULT]
    assert results[0].payload["passed"] is False
    assert results[1].payload["passed"] is True


@pytest.mark.asyncio
async def test_error_is_reported_not_raised():
    bucket, broadcast = collect()

    class Boom(StubExpert):
        async def speak(self, prompt, broadcast):
            raise RuntimeError("壞掉了")

    experts = _experts(pm=["x"], eng=["x"], qa=["x"], senior=["x"])
    experts["pm"] = Boom(BY_KEY["pm"], ["x"])
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")  # 不應拋出
    assert events.EventType.ERROR in types(bucket)
