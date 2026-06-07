"""用 stub 專家測試工作流程狀態機，不呼叫真正的 LLM。"""

from __future__ import annotations

import asyncio

import pytest

from studio import config, events
from studio.orchestrator import (
    StudioSession,
    parse_tasks,
    pm_done,
    qa_passed,
    senior_approved,
)
from studio.roles import BY_KEY, Role


class StubExpert:
    """依角色給腳本化回應，記錄被呼叫次數與收到的 prompt。"""

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


@pytest.fixture(autouse=True)
def _no_debate(monkeypatch):
    """預設關閉架構辯論，讓流程測試專注在逐任務迴圈。"""
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


def types(bucket):
    return [e.type for e in bucket]


def _experts(pm, eng, qa, senior):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }


# --- 決議解析 -----------------------------------------------------------


def test_qa_passed_parsing():
    assert qa_passed("跑了測試\n驗證: PASS")
    assert not qa_passed("有錯\n驗證：FAIL")
    assert qa_passed("一切正常")  # 後備：無明顯失敗字
    assert not qa_passed("test failed")  # 後備：偵測到失敗


def test_senior_parsing():
    assert senior_approved("看起來不錯\n決議: 核可")
    assert not senior_approved("有問題\n決議：退回")


def test_pm_done_parsing():
    assert pm_done("符合\n決議: 完成")
    assert not pm_done("還缺測試\n決議：未完成")


def test_parse_tasks_bullets():
    text = "任務清單:\n- 建立 CLI\n- 加入分類邏輯\n2. 寫說明"
    tasks = parse_tasks(text)
    assert "建立 CLI" in tasks
    assert "寫說明" in tasks
    assert parse_tasks("沒有條列") == ["實作需求"]


def test_parse_tasks_structured():
    text = "任務: 建立 CLI\n任務: 加入分類\n驗收標準: 能跑\n執行指令: python main.py"
    tasks = parse_tasks(text)
    assert tasks == ["建立 CLI", "加入分類"]  # 優先 `任務:`，不含驗收/執行指令


# --- 流程 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_one_task_one_round():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作 BMI", "決議: 完成", "做得不錯，下次可加更多測試"],
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
    assert experts["engineer"].calls == 1


@pytest.mark.asyncio
async def test_retry_then_pass():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討完畢"],
        eng=["第一版", "已修正"],
        qa=["有錯\n驗證: FAIL", "修好了\n驗證: PASS"],
        senior=["先不核可\n決議: 退回", "可以了\n決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 第一輪失敗 → 工程師應被呼叫兩次（同一任務內改進）
    assert experts["engineer"].calls == 2
    results = [e for e in bucket if e.type == events.EventType.RUN_RESULT]
    assert results[0].payload["passed"] is False
    assert results[1].payload["passed"] is True
    # 第二輪工程師 prompt 應帶入回饋意見
    assert "高級工程師審查意見" in experts["engineer"].prompts[1]


@pytest.mark.asyncio
async def test_per_task_iteration_two_tasks():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 建立 A\n任務: 建立 B", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 兩個任務各跑一輪
    assert experts["engineer"].calls == 2
    task_done = [
        e
        for e in bucket
        if e.type == events.EventType.TASK_STATUS and e.payload["status"] == "done"
    ]
    assert {e.payload["id"] for e in task_done} == {1, 2}
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True


@pytest.mark.asyncio
async def test_debate_runs(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["提案", "實作"],
        qa=["驗證: PASS"],
        senior=["點評", "決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "架構討論" in phases
    # 辯論各多一次發言：工程師提案+實作=2、高級工程師點評+審查=2
    assert experts["engineer"].calls == 2
    assert experts["senior"].calls == 2


@pytest.mark.asyncio
async def test_huddle_triggers_on_stall(monkeypatch):
    """任務跑滿輪數仍 FAIL → 觸發 huddle、給 1 輪重試、仍失敗標為已知限制。"""
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["第一版", "再試", "還是這樣", "huddle 後重試"],
        qa=["有錯\n驗證: FAIL"],  # 一律 FAIL（腳本用盡回最後一句）
        senior=["先不核可\n決議: 退回"],  # 一律退回
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    huddles = [e for e in bucket if e.type == events.EventType.HUDDLE]
    # 至少有一次討論事件 + 一次「已知限制」事件
    assert any(not e.payload["limitation"] for e in huddles)
    assert any(e.payload["limitation"] for e in huddles)
    # huddle 後有重試 → 工程師被呼叫次數超過 TASK_MAX_ROUNDS（含 huddle 發言與重試）
    assert experts["engineer"].calls > config.TASK_MAX_ROUNDS
    # 任務最終維持 review（不消失於看板），且被標記限制
    assert session._tasks[0].get("limitation") is True
    task_review = [
        e
        for e in bucket
        if e.type == events.EventType.TASK_STATUS and e.payload["status"] == "review"
    ]
    assert task_review


@pytest.mark.asyncio
async def test_huddle_disabled_by_default():
    """預設不啟用 huddle：滿輪 FAIL 後直接收尾，無 HUDDLE 事件、無重試。"""
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["第一版"],
        qa=["有錯\n驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    assert events.EventType.HUDDLE not in types(bucket)
    assert experts["engineer"].calls == config.TASK_MAX_ROUNDS


@pytest.mark.asyncio
async def test_human_intervention():
    bucket, broadcast = collect()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("請改用公制單位")
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None, intervention_queue=queue)
    await session.run("需求")

    humans = [e for e in bucket if e.type == events.EventType.HUMAN_MESSAGE]
    assert len(humans) == 1
    assert "公制" in humans[0].payload["text"]
    # 插話應前綴進 PM 的拆解 prompt
    assert "使用者插話" in experts["pm"].prompts[0]


@pytest.mark.asyncio
async def test_stop_marks_stopped():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    session.request_stop()
    await session.run("需求")

    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["stopped"] is True
    assert done.payload["completed"] is False
    # 任務迴圈被跳過，工程師不應被呼叫
    assert experts["engineer"].calls == 0


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
