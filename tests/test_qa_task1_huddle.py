"""QA 驗收：任務 #1「卡關 Huddle」驗收標準專測。

驗收標準：
1. 任務連續失敗達 TASK_MAX_ROUNDS → 觸發多角色 huddle 並廣播事件。
2. huddle 後給「剛好 1 輪」重試。
3. 重試仍失敗 → 明確標記「已知限制」（註記 + limitation 事件），非靜默 review。
4. huddle 後重試成功 → 任務 done，不誤標限制。
5. 開關可關閉（預設不啟用）。
"""

from __future__ import annotations

import pytest

from studio import config, events
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
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


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


@pytest.fixture(autouse=True)
def _no_debate(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)


def _experts(pm, eng, qa, senior, architect=None):
    d = {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }
    if architect is not None:
        d["architect"] = StubExpert(BY_KEY["architect"], architect)
    return d


# --- 驗收標準 1+2+3：觸發 + 1 輪重試 + 已知限制 ----------------------------


@pytest.mark.asyncio
async def test_huddle_triggers_and_marks_limitation(monkeypatch):
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["v1", "v2", "v3", "huddle 後重試"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    huddles = [e for e in bucket if e.type == events.EventType.HUDDLE]
    # 觸發討論事件（limitation=False）與已知限制事件（limitation=True）各至少一次
    discuss = [e for e in huddles if not e.payload["limitation"]]
    limited = [e for e in huddles if e.payload["limitation"]]
    assert len(discuss) == 1, "應廣播一次卡關討論事件"
    assert len(limited) == 1, "重試仍失敗應廣播一次『已知限制』事件"

    # 任務被明確標記限制，且仍留在看板（status=review，非 done、非消失）
    assert session._tasks[0].get("limitation") is True
    assert session._tasks[0]["status"] == "review"

    # 觸發條件：第一輪迴圈跑滿 TASK_MAX_ROUNDS 才召集
    # 工程師發言 = 主迴圈滿輪 + huddle 1 次發言 + 重試 1 輪
    assert experts["engineer"].calls == config.TASK_MAX_ROUNDS + 2


@pytest.mark.asyncio
async def test_huddle_retry_is_exactly_one_round(monkeypatch):
    """huddle 後重試只給 1 輪：重試也失敗時不會再多跑。"""
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 2)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["v1", "v2", "huddle 發言", "重試"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 主迴圈 2 輪 + huddle 1 次 + 重試「1 輪」= 4，證明重試不超過 1 輪
    assert experts["engineer"].calls == 2 + 1 + 1


# --- 驗收標準：參與者組成 ------------------------------------------------


@pytest.mark.asyncio
async def test_huddle_participants_include_architect_when_present(monkeypatch):
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["v1", "v2", "v3", "重試"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
        architect=["架構觀點"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    discuss = [
        e for e in bucket if e.type == events.EventType.HUDDLE and not e.payload["limitation"]
    ][0]
    parts = discuss.payload["participants"]
    assert parts == ["pm", "architect", "engineer", "senior"]


@pytest.mark.asyncio
async def test_huddle_skips_absent_architect(monkeypatch):
    """缺席角色（無架構師）自動略過，不崩潰。"""
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["v1", "v2", "v3", "重試"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    discuss = [
        e for e in bucket if e.type == events.EventType.HUDDLE and not e.payload["limitation"]
    ][0]
    assert "architect" not in discuss.payload["participants"]
    assert discuss.payload["participants"] == ["pm", "engineer", "senior"]


# --- 驗收標準 4：huddle 後重試成功 → done，不誤標限制 --------------------


@pytest.mark.asyncio
async def test_huddle_retry_success_marks_done(monkeypatch):
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    bucket, broadcast = collect()
    # 主迴圈一律失敗（滿輪），重試輪 QA/senior 通過
    qa_scripts = ["驗證: FAIL"] * config.TASK_MAX_ROUNDS + ["驗證: PASS"]
    senior_scripts = ["決議: 退回"] * config.TASK_MAX_ROUNDS + ["決議: 核可"]
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["v"],  # 用盡回最後一句
        qa=qa_scripts,
        senior=senior_scripts,
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 重試成功 → 沒有「已知限制」事件，任務 done 無 limitation 註記
    limited = [e for e in bucket if e.type == events.EventType.HUDDLE and e.payload["limitation"]]
    assert limited == []
    assert session._tasks[0].get("limitation") is not True
    assert session._tasks[0]["status"] == "done"
    # 仍有一次卡關討論事件（確實觸發過 huddle）
    assert any(e.type == events.EventType.HUDDLE and not e.payload["limitation"] for e in bucket)


# --- 驗收標準 5：開關預設關閉 -------------------------------------------


@pytest.mark.asyncio
async def test_huddle_off_by_default():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["v1"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    assert events.EventType.HUDDLE not in [e.type for e in bucket]
    assert experts["engineer"].calls == config.TASK_MAX_ROUNDS
    # 未啟用時任務仍維持 review（既有行為），不標限制
    assert session._tasks[0].get("limitation") is not True
    assert session._tasks[0]["status"] == "review"


# --- huddle 結論注入重試 prompt --------------------------------------


@pytest.mark.asyncio
async def test_huddle_conclusion_seeded_into_retry(monkeypatch):
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["v1", "v2", "v3", "重試實作"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 工程師最後一次（重試）prompt 應帶入 huddle 替代方案
    retry_prompt = experts["engineer"].prompts[-1]
    assert "卡關 huddle 替代方案" in retry_prompt
