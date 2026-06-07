"""QA 驗收：任務 #2「異議 critic」驗收標準專測。

驗收標準：
1. 驗收/審查放行前「一定」經過 critic 關卡（qa+senior 通過後，仍須 critic 放行才算 done）。
2. critic 提出實質反對（異議: 成立）→ 流程退回再修。
3. 無反對（異議: 不成立）→ 通過。
4. 「退回」與「放行」兩路徑皆有覆蓋。
補充驗證：
- 任務 gate 用 pm 視角、最終 gate 用 senior 視角（換人保獨立）。
- critic prompt 刻意不餵當事人的核可理由（反錨定）。
- 可關閉（CRITIC_ENABLED 預設 False）。
"""

from __future__ import annotations

import pytest

from studio import config, events
from studio.orchestrator import StudioSession, critic_blocks
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


def _experts(pm, eng, qa, senior):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }


def _happy():
    return _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了", "依異議修正"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )


# --- 判定純函式 ---------------------------------------------------------


def test_critic_blocks_parsing():
    assert critic_blocks("異議: 成立")
    assert critic_blocks("異議：成立")  # 全形冒號
    assert not critic_blocks("異議: 不成立")
    assert not critic_blocks("看起來很完整")  # 後備：無反對 → 放行
    assert critic_blocks("這還不算完成")  # 後備：明確反對 → 退回
    # 多次標記取最後一次
    assert not critic_blocks("先說異議: 成立\n再想想\n異議: 不成立")


# --- 驗收標準 1：放行前「一定」經過 critic ------------------------------


@pytest.mark.asyncio
async def test_task_gate_runs_before_done(monkeypatch):
    """qa+senior 都通過，但任務標 done 前一定先發出 CRITIC_REVIEW 事件。"""
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _happy()
    critics = {
        "pm": StubExpert(BY_KEY["pm"], ["異議: 不成立"]),
        "senior": StubExpert(BY_KEY["senior"], ["異議: 不成立"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    seq = [e.type for e in bucket]
    # 任務 done 的 task_status 之前，必先出現一次 critic_review
    first_critic = seq.index(events.EventType.CRITIC_REVIEW)
    task_done_idx = next(
        i
        for i, e in enumerate(bucket)
        if e.type == events.EventType.TASK_STATUS and e.payload["status"] == "done"
    )
    assert first_critic < task_done_idx, "critic 關卡必須在任務放行(done)之前"
    # 任務 gate 確實呼叫了 pm 視角 critic
    assert critics["pm"].calls >= 1


# --- 驗收標準 2+4：退回路徑 --------------------------------------------


@pytest.mark.asyncio
async def test_critic_block_then_release(monkeypatch):
    """critic 異議成立 → 退回；工程師多改一輪；第二次不成立 → 放行。"""
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _happy()
    critics = {"pm": StubExpert(BY_KEY["pm"], ["異議: 成立，缺邊界處理", "異議: 不成立"])}
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    reviews = [e for e in bucket if e.type == events.EventType.CRITIC_REVIEW]
    assert reviews[0].payload["passed"] is False  # 第一次退回
    assert reviews[-1].payload["passed"] is True  # 最後放行
    # 退回 → 工程師被迫再改一輪，且退回理由帶進下一輪 prompt
    assert experts["engineer"].calls == 2
    assert "異議檢查" in experts["engineer"].prompts[1]
    # 退回階段有可觀察的 phase_change
    phases = [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "異議退回" in phases


# --- 驗收標準 3+4：放行路徑 --------------------------------------------


@pytest.mark.asyncio
async def test_critic_release_first_time(monkeypatch):
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _happy()
    critics = {
        "pm": StubExpert(BY_KEY["pm"], ["異議: 不成立"]),
        "senior": StubExpert(BY_KEY["senior"], ["異議: 不成立"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    reviews = [e for e in bucket if e.type == events.EventType.CRITIC_REVIEW]
    assert reviews and all(e.payload["passed"] for e in reviews)
    assert experts["engineer"].calls == 1  # 放行不增加輪數
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True


# --- 最終驗收 gate：退回 → 整體未完成 ----------------------------------


@pytest.mark.asyncio
async def test_final_gate_blocks_completion(monkeypatch):
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _happy()
    critics = {
        "pm": StubExpert(BY_KEY["pm"], ["異議: 不成立"]),  # 任務 gate 放行
        "senior": StubExpert(BY_KEY["senior"], ["異議: 成立，整合未驗證"]),  # 最終 gate 退回
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is False
    # 最終 gate 確實用 senior 視角
    final_reviews = [
        e
        for e in bucket
        if e.type == events.EventType.CRITIC_REVIEW and e.payload["gate"] == "senior"
    ]
    assert final_reviews and final_reviews[-1].payload["passed"] is False


# --- 換人原則：任務 gate=pm、最終 gate=senior --------------------------


@pytest.mark.asyncio
async def test_gate_perspectives_are_split(monkeypatch):
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _happy()
    critics = {
        "pm": StubExpert(BY_KEY["pm"], ["異議: 不成立"]),
        "senior": StubExpert(BY_KEY["senior"], ["異議: 不成立"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    gates = [e.payload["gate"] for e in bucket if e.type == events.EventType.CRITIC_REVIEW]
    assert "pm" in gates  # 任務審查 gate
    assert "senior" in gates  # 最終驗收 gate
    # 兩個 critic 各自獨立呼叫（不共用對話序號）
    assert critics["pm"].calls == 1
    assert critics["senior"].calls == 1


# --- 反錨定：critic prompt 不含當事人核可理由 --------------------------


@pytest.mark.asyncio
async def test_critic_prompt_excludes_approver_reasoning(monkeypatch):
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS，所有案例都過了"],
        senior=["品質極佳毫無問題\n決議: 核可"],
    )
    critics = {
        "pm": StubExpert(BY_KEY["pm"], ["異議: 不成立"]),
        "senior": StubExpert(BY_KEY["senior"], ["異議: 不成立"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    # critic 不應看到 senior 的核可理由（避免錨定）
    pm_critic_prompt = critics["pm"].prompts[0]
    assert "品質極佳毫無問題" not in pm_critic_prompt
    assert "獨立的異議檢查者" in pm_critic_prompt


# --- 驗收標準：可關閉（預設） ------------------------------------------


@pytest.mark.asyncio
async def test_critic_disabled_by_default():
    bucket, broadcast = collect()
    experts = _happy()
    critics = {"pm": StubExpert(BY_KEY["pm"], ["異議: 成立"])}
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    assert events.EventType.CRITIC_REVIEW not in [e.type for e in bucket]
    assert critics["pm"].calls == 0  # 停用時完全不呼叫 critic
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True  # 行為與舊版一致
