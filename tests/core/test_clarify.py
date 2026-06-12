"""需求澄清階段測試：parse_clarify 解析、澄清流程（回覆/逾時/停用/無佇列）、PRD.md 固化。

用 stub 專家、不呼叫 LLM；等待逾時用極短 CLARIFY_TIMEOUT，測試不卡。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import config, events
from studio.orchestrator import StudioSession, parse_clarify
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


@pytest.fixture(autouse=True)
def _fast_flow(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    # 釘循序：並行模式下 cwd!=None 的 lane 會經 providers.make_expert 建「真」專家
    # （繞過注入的 stub），測試會真的起 SDK 子程序而卡死。
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "CLARIFY_ENABLED", True)
    monkeypatch.setattr(config, "CLARIFY_TIMEOUT", 0.05)  # 逾時路徑毫秒級，不卡測試


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


_CLARIFY_REPLY = (
    "問題: 要支援哪些平台？\n假設: 先做 Linux/macOS\n問題: 要不要圖形介面？\n假設: 先做 CLI"
)

# PM 腳本：澄清 → 拆解 → 驗收 → 檢討
_PM = [_CLARIFY_REPLY, "任務: 實作", "決議: 完成", "檢討"]
_PM_NO_CLARIFY = ["澄清: 不需要", "任務: 實作", "決議: 完成", "檢討"]


def _experts(pm_scripts):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts),
        "engineer": StubExpert(BY_KEY["engineer"], ["做好了"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }


# --- 解析 ---------------------------------------------------------------


def test_parse_clarify_pairs_questions_with_assumptions():
    out = parse_clarify(_CLARIFY_REPLY)
    assert [q["q"] for q in out] == ["要支援哪些平台？", "要不要圖形介面？"]
    assert out[0]["assumption"] == "先做 Linux/macOS"
    assert out[1]["assumption"] == "先做 CLI"


def test_parse_clarify_not_needed():
    assert parse_clarify("需求很清楚。\n澄清: 不需要") == []
    assert parse_clarify("隨意聊聊，沒有問題行") == []


def test_parse_clarify_assumption_without_question_ignored():
    assert parse_clarify("假設: 孤兒假設行") == []


# --- 流程 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarify_answer_folded_into_decompose_prompt():
    bucket, broadcast = collect()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("平台只要 Linux；不要 GUI")  # 預先放好回覆
    experts = _experts(_PM)
    session = StudioSession("t", broadcast, experts=experts, cwd=None, intervention_queue=queue)
    await session.run("做一個無人機地面站")

    # 有 clarify_request 事件且帶問題與逾時
    reqs = [e for e in bucket if e.type == events.EventType.CLARIFY_REQUEST]
    assert len(reqs) == 1
    assert [q["q"] for q in reqs[0].payload["questions"]] == [
        "要支援哪些平台？",
        "要不要圖形介面？",
    ]
    assert reqs[0].payload["timeout_s"] == config.CLARIFY_TIMEOUT

    # 回覆被折進拆解 prompt（pm 第 2 次發言），且標明覆蓋假設
    decompose_prompt = experts["pm"].prompts[1]
    assert "需求澄清" in decompose_prompt
    assert "平台只要 Linux" in decompose_prompt
    assert "以此為準" in decompose_prompt


@pytest.mark.asyncio
async def test_clarify_timeout_proceeds_with_assumptions():
    bucket, broadcast = collect()
    queue: asyncio.Queue[str] = asyncio.Queue()  # 沒人回覆
    experts = _experts(_PM)
    session = StudioSession("t", broadcast, experts=experts, cwd=None, intervention_queue=queue)
    await session.run("做一個無人機地面站")

    decompose_prompt = experts["pm"].prompts[1]
    assert "未獲回覆" in decompose_prompt
    assert "先做 Linux/macOS" in decompose_prompt  # 假設被帶進拆解
    # 流程照常完成
    done = [e for e in bucket if e.type == events.EventType.DONE]
    assert done and done[-1].payload["completed"] is True


@pytest.mark.asyncio
async def test_clarify_not_needed_skips_wait():
    bucket, broadcast = collect()
    queue: asyncio.Queue[str] = asyncio.Queue()
    experts = _experts(_PM_NO_CLARIFY)
    session = StudioSession("t", broadcast, experts=experts, cwd=None, intervention_queue=queue)
    await session.run("做一個四則運算 CLI，pytest 驗證")

    assert not [e for e in bucket if e.type == events.EventType.CLARIFY_REQUEST]
    # 拆解 prompt 不含澄清段
    assert "需求澄清" not in experts["pm"].prompts[1]


@pytest.mark.asyncio
async def test_clarify_skipped_without_queue_or_when_disabled(monkeypatch):
    # 無插話佇列（autopilot 型態）：PM 第一次發言就是拆解
    bucket, broadcast = collect()
    experts = _experts(["任務: 實作", "決議: 完成", "檢討"])
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")
    assert not [e for e in bucket if e.type == events.EventType.CLARIFY_REQUEST]
    assert "任務" in experts["pm"].prompts[0]  # 直接收到拆解指示

    # 顯式關閉（持續改良迴圈傳 clarify=False）：有佇列也跳過
    bucket2, broadcast2 = collect()
    experts2 = _experts(["任務: 實作", "決議: 完成", "檢討"])
    session2 = StudioSession(
        "t2",
        broadcast2,
        experts=experts2,
        cwd=None,
        intervention_queue=asyncio.Queue(),
        clarify=False,
    )
    await session2.run("需求")
    assert not [e for e in bucket2 if e.type == events.EventType.CLARIFY_REQUEST]


@pytest.mark.asyncio
async def test_clarify_writes_prd(tmp_path):
    _, broadcast = collect()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("只要 Linux")
    experts = _experts(_PM)
    session = StudioSession("t", broadcast, experts=experts, cwd=tmp_path, intervention_queue=queue)
    await session.run("做一個無人機地面站")

    prd = (tmp_path / "PRD.md").read_text(encoding="utf-8")
    assert "產品需求紀錄" in prd
    assert "做一個無人機地面站" in prd
    assert "要支援哪些平台？" in prd
    assert "只要 Linux" in prd


@pytest.mark.asyncio
async def test_prd_appends_across_sessions(tmp_path):
    """專案模式：同一 workspace 連跑兩場，PRD.md 追加不覆蓋。"""
    for i, req in enumerate(["第一版需求", "第二版需求"]):
        _, broadcast = collect()
        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait(f"回覆 {i}")
        experts = _experts(_PM)
        session = StudioSession(
            f"t{i}", broadcast, experts=experts, cwd=tmp_path, intervention_queue=queue
        )
        await session.run(req)
    prd = (tmp_path / "PRD.md").read_text(encoding="utf-8")
    assert "第一版需求" in prd and "第二版需求" in prd
    assert prd.count("產品需求紀錄") == 1  # 檔頭只寫一次


# --- 願景回填（與澄清同一發言抽出，給專案 meta 用）------------------------


@pytest.mark.asyncio
async def test_vision_extracted_into_result(tmp_path, monkeypatch):
    """PM 澄清發言含 `願景:` 行 → result["vision"] 抽出；無標記回空字串。"""
    from studio import workspace

    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    sid = "vis1"
    workspace.create_workspace(sid)
    _bucket, broadcast = collect()
    queue: asyncio.Queue[str] = asyncio.Queue()
    experts = _experts(["澄清: 不需要\n願景: 最輕量的記帳工具", "任務: 實作", "決議: 完成", "檢討"])
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        intervention_queue=queue,
    )
    result = await session.run("做一個記帳工具")
    assert result["vision"] == "最輕量的記帳工具"

    # 無願景行：回空字串（ws 端不會回填）
    sid2 = "vis2"
    workspace.create_workspace(sid2)
    experts2 = _experts(["澄清: 不需要", "任務: 實作", "決議: 完成", "檢討"])
    session2 = StudioSession(
        sid2,
        broadcast,
        experts=experts2,
        cwd=workspace.workspace_path(sid2),
        intervention_queue=asyncio.Queue(),
    )
    result2 = await session2.run("需求")
    assert result2["vision"] == ""
