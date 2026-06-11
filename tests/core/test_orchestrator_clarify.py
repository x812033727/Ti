"""端到端驗證：立項/需求澄清階段（_inception）在真實 orchestrator 流程的接線。

沿用 tests/test_lessons_e2e.py 的 StubExpert 模式（不需 LLM / bwrap）。涵蓋：
不需澄清直接出 PRD、需澄清＋使用者回覆、需澄清＋逾時走假設、停用/無 queue 行為不變、
等待中停止可秒級退出、PRD 沉澱與後續任務回填。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import config, events, workspace
from studio.orchestrator import (
    StudioSession,
    parse_clarify_needed,
    parse_questions,
    parse_vision,
)
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


def _collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


def _experts(pm_scripts: list[str]):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts),
        "engineer": StubExpert(BY_KEY["engineer"], ["做好了"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }


@pytest.fixture(autouse=True)
def _base_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "CLARIFY_ENABLED", True)
    monkeypatch.setattr(config, "CLARIFY_TIMEOUT", 1)
    monkeypatch.setattr(config, "KNOWLEDGE_ENABLED", True)


_PRD = (
    "目標用戶: 個人記帳者\nMVP 範圍: 命令列記帳\n不做什麼: 不做雲端同步\n"
    "假設: 單一使用者\n願景: 最輕量的記帳工具\n後續任務: 加上匯出 CSV\n澄清: 不需要"
)


# ---------- 解析函式 ----------


def test_parse_markers():
    assert parse_clarify_needed("問題: 目標平台？\n澄清: 需要") is True
    assert parse_clarify_needed(_PRD) is False
    assert parse_clarify_needed("沒有標記的文字") is False  # 無標記保守不卡等待
    assert parse_questions("問題: A？\n問題: B？\n問題: C？\n問題: D？") == ["A？", "B？", "C？"]
    assert parse_vision(_PRD) == "最輕量的記帳工具"
    assert parse_vision("沒有願景行") == ""


# ---------- 不需澄清 ----------


@pytest.mark.asyncio
async def test_clear_requirement_writes_prd_no_wait():
    """PM 回『不需要』：不等待、PRD 落盤 docs/PRD.md、拆解 prompt 含 PRD、後續任務回填。"""
    sid = "c1"
    workspace.create_workspace(sid)
    bucket, broadcast = _collect()
    experts = _experts([_PRD, "任務: 實作記帳核心", "決議: 完成", "檢討：無"])
    queue: asyncio.Queue[str] = asyncio.Queue()
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        intervention_queue=queue,
    )
    result = await session.run("做一個記帳工具")

    assert "最輕量的記帳工具" in workspace.read_doc_tail(sid, "PRD.md", 4000)
    assert "docs/PRD.md" in workspace.list_files(sid)
    # 拆解 prompt（PM 第二次發言）帶 PRD
    assert "【PRD（立項結論）】" in experts["pm"].prompts[1]
    assert "個人記帳者" in experts["pm"].prompts[1]
    # 超範圍項回填後續任務、願景進結果
    assert "加上匯出 CSV" in result["followups"]
    assert result["vision"] == "最輕量的記帳工具"
    # 不該發出 clarify_request
    assert not [e for e in bucket if e.type == events.EventType.CLARIFY_REQUEST]


# ---------- 需澄清 ----------


@pytest.mark.asyncio
async def test_clarify_with_user_reply():
    """PM 回『需要』＋queue 預塞回覆：發 clarify_request，第二次發言帶使用者回覆。"""
    sid = "c2"
    workspace.create_workspace(sid)
    bucket, broadcast = _collect()
    experts = _experts(
        [
            "問題: 要做網頁版還是命令列？\n澄清: 需要",
            _PRD,
            "任務: 實作核心",
            "決議: 完成",
            "檢討：無",
        ]
    )
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("命令列就好")
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        intervention_queue=queue,
    )
    await session.run("做個記帳的東西")

    reqs = [e for e in bucket if e.type == events.EventType.CLARIFY_REQUEST]
    assert len(reqs) == 1
    assert reqs[0].payload["questions"] == ["要做網頁版還是命令列？"]
    # PM 第二次發言（寫 PRD）帶使用者回覆
    assert "命令列就好" in experts["pm"].prompts[1]
    # PRD 仍照常沉澱
    assert "最輕量的記帳工具" in workspace.read_doc_tail(sid, "PRD.md", 4000)


@pytest.mark.asyncio
async def test_clarify_timeout_falls_back_to_assumptions():
    """PM 回『需要』＋無人回覆：逾時後走明示假設路徑，不卡死。"""
    sid = "c3"
    workspace.create_workspace(sid)
    bucket, broadcast = _collect()
    experts = _experts(["問題: 平台？\n澄清: 需要", _PRD, "任務: 實作", "決議: 完成", "檢討：無"])
    queue: asyncio.Queue[str] = asyncio.Queue()  # 空 queue＝無人回覆
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        intervention_queue=queue,
    )
    await asyncio.wait_for(session.run("做個東西"), timeout=10)

    assert "保守假設" in experts["pm"].prompts[1]
    phases = [
        e.payload.get("detail", "") for e in bucket if e.type == events.EventType.PHASE_CHANGE
    ]
    assert any("未收到回覆" in d for d in phases)


@pytest.mark.asyncio
async def test_stop_during_wait_exits_quickly(monkeypatch):
    """等待回覆期間停止：秒級退出（分段輪詢 stop，不等滿逾時）。"""
    monkeypatch.setattr(config, "CLARIFY_TIMEOUT", 600)
    sid = "c4"
    workspace.create_workspace(sid)
    _bucket, broadcast = _collect()
    experts = _experts(["問題: 平台？\n澄清: 需要", "任務: 實作", "決議: 完成", "檢討：無"])
    queue: asyncio.Queue[str] = asyncio.Queue()
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        intervention_queue=queue,
    )

    async def _stop_soon():
        await asyncio.sleep(0.2)
        session.request_stop()

    asyncio.ensure_future(_stop_soon())
    # CLARIFY_TIMEOUT=600 下仍須秒級結束（分段輪詢 stop）；停止語義沿用既有流程（DONE 照發）。
    await asyncio.wait_for(session.run("做個東西"), timeout=5)
    done = [e for e in _bucket if e.type == events.EventType.DONE][0]
    assert done.payload["stopped"] is True
    # 等待中被停止：PM 不再被要求寫 PRD（立項只發言一次，第二次是既有的拆解發言）。
    assert all("請寫出簡短 PRD" not in p for p in experts["pm"].prompts)


# ---------- 跳過條件（行為不變）----------


@pytest.mark.asyncio
async def test_disabled_or_no_queue_behaves_as_before(monkeypatch):
    """關閉開關／無 intervention queue：PM 第一句就是拆解（與舊行為逐字相同）。"""
    for setup in ("disabled", "noqueue"):
        if setup == "disabled":
            monkeypatch.setattr(config, "CLARIFY_ENABLED", False)
            queue: asyncio.Queue[str] | None = asyncio.Queue()
        else:
            monkeypatch.setattr(config, "CLARIFY_ENABLED", True)
            queue = None
        sid = f"c5{setup}"
        workspace.create_workspace(sid)
        _bucket, broadcast = _collect()
        experts = _experts(["任務: 實作", "決議: 完成", "檢討：無"])
        session = StudioSession(
            sid,
            broadcast,
            experts=experts,
            cwd=workspace.workspace_path(sid),
            intervention_queue=queue,
        )
        await session.run("需求")
        assert "請拆解成結構化任務清單" in experts["pm"].prompts[0]
        assert "立項評估" not in experts["pm"].prompts[0]
        assert "docs/PRD.md" not in workspace.list_files(sid)
